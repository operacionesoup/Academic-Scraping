# oup_academic_server.py
# ─────────────────────────────────────────────────────────────────────────────
# FastAPI + Playwright — Scraper de precios Oxford University Press (Academic)
#
# Estrategia:
#   1. Va directamente a la URL de búsqueda con el ISBN (sin pasar por la home)
#   2. Acepta cookies (sólo la primera vez por proceso — persisten en el contexto)
#   3. Bloquea imágenes / fuentes / CSS para acelerar la carga
#   4. En los resultados, clic en el enlace del producto
#   5. Extrae título, precio, moneda e ISBN de la página de producto
#
# Selectores DevTools (página de producto):
#   Título:  h1.product_biblio_title
#   Precio:  span[itemprop="price"]
#   Moneda:  span[itemprop="priceCurrency"] → atributo content="GBP"
#   ISBN:    <p>ISBN: XXXXX</p> en div.content_right.product_sidebar
#
# Instalación:
#   pip install fastapi uvicorn playwright
#   playwright install chromium
#
# Arranque:
#   uvicorn oup_academic_server:app --reload --port 8003
#
# Docs interactivas: http://localhost:8003/docs
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import re
import asyncio
from typing import Optional, List, Dict, Any

app = FastAPI(
    title="Oxford University Press — Academic Price Scraper",
    description="Obtiene el precio, título e ISBN de libros en global.oup.com/academic a partir de su ISBN.",
    version="4.0.0",
)

# ─── Estado global del navegador ─────────────────────────────────────────────
_pw      = None
_browser = None
_context = None

# Las cookies persisten en _context: sólo hay que aceptarlas una vez
_cookies_accepted = False

# n8n envía lotes de 5 ISBNs por llamada. Semaphore=5 para procesarlos en paralelo.
sem = asyncio.Semaphore(5)

BASE_URL   = "https://global.oup.com/academic/?lang=en&cc=gb"
# Ir directamente a búsqueda evita cargar la home + interactuar con el formulario
SEARCH_URL = "https://global.oup.com/academic/search/?q={isbn}&lang=en&cc=gb"

# Tipos de recurso que no necesitamos: bloquearlos acelera la carga ~40%
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


# ─── Utilidades ─────────────────────────────────────────────────────────────

def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "").replace("-", "")


def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d{2,3})?)", text)
    if not m:
        return None
    return m.group(1).replace(",", ".")


def extract_currency(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"([£$€])", text)
    return m.group(1) if m else None


# ─── Lifecycle del navegador ─────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global _pw, _browser, _context
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    _context = await _browser.new_context(
        locale="en-GB",
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    )


@app.on_event("shutdown")
async def shutdown():
    global _pw, _browser, _context
    try:
        if _context: await _context.close()
        if _browser: await _browser.close()
        if _pw:      await _pw.stop()
    except Exception:
        pass


# ─── Helpers de página ───────────────────────────────────────────────────────

async def _block_resources(route, request) -> None:
    if request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


async def accept_cookies(page) -> None:
    """
    Acepta el banner de cookies de OUP Academic.
    Sólo actúa si todavía no se aceptaron en este proceso
    (las cookies persisten en el contexto compartido).
    """
    global _cookies_accepted
    if _cookies_accepted:
        return

    for selector in [
        "#onetrust-accept-btn-handler",
        "button:has-text('Aceptar todas las cookies')",
        "button:has-text('Accept All Cookies')",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.count() > 0 and await btn.is_visible(timeout=2_000):
                await btn.click(timeout=3_000)
                _cookies_accepted = True
                return
        except Exception:
            continue


# ─── Scraping core ───────────────────────────────────────────────────────────

async def scrape_academic_one(isbn: str) -> Dict[str, Any]:
    """
    Scrapea un producto de global.oup.com/academic yendo directamente
    a la URL de búsqueda (sin pasar por la home page).
    """
    global _context
    isbn = clean_isbn(isbn)

    if not (10 <= len(isbn) <= 13) or not isbn.isdigit():
        return {
            "isbn": isbn, "title": None, "price": None,
            "currency": None, "url": BASE_URL,
            "error": "ISBN inválido (debe tener 10-13 dígitos numéricos)",
        }

    async with sem:
        page = await _context.new_page()
        # Bloquear recursos innecesarios antes de cualquier navegación
        await page.route("**/*", _block_resources)
        try:
            # ═════════════════════════════════════════════════════════════
            # PASO 1: Ir directamente a la página de resultados de búsqueda
            # Evita cargar la home + interactuar con el formulario (~7s)
            # ═════════════════════════════════════════════════════════════
            await page.goto(
                SEARCH_URL.format(isbn=isbn),
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            # ═════════════════════════════════════════════════════════════
            # PASO 2: Aceptar cookies (no-op si ya se aceptaron)
            # ═════════════════════════════════════════════════════════════
            await accept_cookies(page)

            # ═════════════════════════════════════════════════════════════
            # PASO 3: Navegar al producto
            # Puede que el buscador aterrice directamente en el producto
            # o en una lista de resultados.
            # ═════════════════════════════════════════════════════════════
            product_loaded = await page.locator("h1.product_biblio_title").count() > 0

            if not product_loaded:
                # Solo clic en links que contienen el ISBN exacto
                link = page.locator(f"a[href*='{isbn}']").first
                if await link.count() > 0:
                    await link.click(timeout=10_000)
                    await page.wait_for_load_state("domcontentloaded")
                    await accept_cookies(page)
                    product_loaded = True

            if "amazon" in page.url:
                return {
                    "isbn": isbn, "title": None, "price": None,
                    "currency": None, "url": page.url,
                    "error": "Redirigió a Amazon",
                }

            if not product_loaded:
                try:
                    await page.locator("h1.product_biblio_title").first.wait_for(timeout=5_000)
                    product_loaded = True
                except Exception:
                    return {
                        "isbn": isbn, "title": None, "price": None,
                        "currency": None, "url": page.url,
                        "error": "Producto no encontrado para este ISBN",
                    }

            # ═════════════════════════════════════════════════════════════
            # PASO 4: EXTRACCIÓN DE DATOS
            # ═════════════════════════════════════════════════════════════

            # ── TÍTULO ───────────────────────────────────────────────────
            title = None
            try:
                title = (await page.locator("h1.product_biblio_title").first.inner_text(timeout=5_000)).strip()
            except Exception:
                try:
                    title = (await page.locator('h1[itemprop="name"]').first.inner_text(timeout=5_000)).strip()
                except Exception:
                    pass

            # ── PRECIO ───────────────────────────────────────────────────
            price = None
            try:
                ps = page.locator('span[itemprop="price"]').first
                await ps.wait_for(timeout=10_000)
                raw = (await ps.inner_text(timeout=5_000)).strip()
                if raw:
                    price = raw.replace(",", ".")
            except Exception:
                pass

            if not price:
                try:
                    raw = (await page.locator("p.product_price").first.inner_text(timeout=5_000)).strip()
                    price = normalize_price(raw)
                except Exception:
                    pass

            if not price:
                try:
                    html = await page.content()
                    m = re.search(r'itemprop="price"[^>]*>(\d{1,4}[.,]\d{2})<', html)
                    if m:
                        price = m.group(1).replace(",", ".")
                except Exception:
                    pass

            # ── MONEDA ───────────────────────────────────────────────────
            currency = None
            try:
                code = await page.locator('span[itemprop="priceCurrency"]').first.get_attribute(
                    "content", timeout=3_000
                )
                if code:
                    currency = {"GBP": "£", "USD": "$", "EUR": "€"}.get(code.strip(), code.strip())
            except Exception:
                pass

            if not currency:
                try:
                    txt = (await page.locator("p.product_price").first.inner_text(timeout=3_000)).strip()
                    currency = extract_currency(txt)
                except Exception:
                    pass

            # ── ISBN (desde la página) ───────────────────────────────────
            page_isbn = isbn
            try:
                sidebar_ps = page.locator("div.content_right.product_sidebar p")
                for i in range(await sidebar_ps.count()):
                    try:
                        txt = (await sidebar_ps.nth(i).inner_text(timeout=2_000)).strip()
                        if txt.startswith("ISBN:"):
                            m = re.search(r"(\d{10,13})", txt)
                            if m:
                                page_isbn = m.group(1)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # ── Resultado final ──────────────────────────────────────────
            if not price:
                return {
                    "isbn": page_isbn, "title": title, "price": None,
                    "currency": None, "url": page.url,
                    "error": "Precio no encontrado en la página",
                }

            return {
                "isbn": page_isbn,
                "title": title,
                "price": price,
                "currency": currency,
                "url": page.url,
                "error": None,
            }

        except PlaywrightTimeoutError:
            return {
                "isbn": isbn, "title": None, "price": None,
                "currency": None, "url": BASE_URL,
                "error": f"Timeout ({isbn})",
            }
        except Exception as e:
            return {
                "isbn": isbn, "title": None, "price": None,
                "currency": None, "url": BASE_URL,
                "error": str(e),
            }
        finally:
            await page.close()


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "message": "OUP Academic Scraper API running",
        "docs": "/docs",
        "health": "/health",
        "version": "/version",
        "test_endpoint": "/test",
        "single_scrape_example": "/oup/academic?isbn=9780199537006",
    }

@app.get("/test")
async def test_isbn():
    result = await scrape_academic_one("9780199537006")
    return {"isbn_test": "9780199537006", "result": result}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/version")
async def version():
    return {"version": "4.0.0", "source": "oup_academic"}


class OUPAcademicResult(BaseModel):
    isbn: str
    title: Optional[str]
    price: Optional[str]
    currency: Optional[str]
    url: str
    error: Optional[str]


@app.get(
    "/oup/academic",
    response_model=OUPAcademicResult,
    summary="Precio de un libro OUP Academic por ISBN",
)
async def oup_academic_single(
    isbn: str = Query(..., description="ISBN-10 o ISBN-13 del libro"),
):
    return await scrape_academic_one(isbn)


class BatchRequest(BaseModel):
    isbns: List[str] = Field(
        ..., min_length=1, max_length=50,
        description="Lista de ISBNs (máx. 50 por petición)",
        json_schema_extra={"examples": [["9780199537006", "9780198826736"]]},
    )

class BatchResponse(BaseModel):
    source: str
    count: int
    results: List[OUPAcademicResult]


@app.post(
    "/oup/academic/batch",
    response_model=BatchResponse,
    summary="Precio de múltiples libros OUP Academic por lista de ISBNs",
)
async def oup_academic_batch(req: BatchRequest):
    isbns = [clean_isbn(x) for x in req.isbns if clean_isbn(x)]
    if not isbns:
        return {"source": "oup_academic", "count": 0, "results": []}

    tasks   = [scrape_academic_one(isbn) for isbn in isbns]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    final: List[Dict[str, Any]] = []
    for isbn, r in zip(isbns, results):
        if isinstance(r, Exception):
            final.append({
                "isbn": isbn, "title": None, "price": None,
                "currency": None, "url": BASE_URL, "error": str(r),
            })
        else:
            final.append(r)

    return {"source": "oup_academic", "count": len(final), "results": final}
