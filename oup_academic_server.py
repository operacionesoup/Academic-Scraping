# oup_academic_server.py
# ─────────────────────────────────────────────────────────────────────────────
# FastAPI + Playwright — Scraper de precios Oxford University Press (Academic)
# URL patrón: https://global.oup.com/academic/product/{slug}-{ISBN}?cc=es&lang=en
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
    version="2.0.0",
)

# ─── Estado global del navegador (se comparte entre requests) ────────────────
_pw      = None
_browser = None
_context = None

# Máximo 2 páginas simultáneas para no saturar
sem = asyncio.Semaphore(2)


# ─── Utilidades ─────────────────────────────────────────────────────────────

def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "").replace("-", "")


def build_url(isbn: str) -> str:
    """
    Usa el buscador interno de OUP Academic con el ISBN como query param.
    OUP redirige automáticamente al producto si lo encuentra.
    """
    return f"https://global.oup.com/academic/?q={isbn}&lang=en&cc=es"


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

async def accept_cookies(page) -> None:
    """
    Cierra el banner de cookies de OUP Academic.
    El botón dice: "Aceptar todas las cookies"
    """
    for attempt in range(5):
        try:
            btn = page.locator(
                "button:has-text('Aceptar todas las cookies')"
            ).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            pass

        try:
            btn = page.get_by_role(
                "button",
                name=re.compile(r"(accept all|aceptar todas|accept|aceptar)", re.I)
            ).first
            if await btn.count() > 0:
                await btn.click(timeout=3000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            pass

        try:
            for sel in [
                "#onetrust-accept-btn-handler",
                ".cookie-accept",
                "[data-testid='accept-cookies']",
            ]:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(500)
                    return
        except Exception:
            pass

        await page.wait_for_timeout(1000)


# ─── Scraping core ───────────────────────────────────────────────────────────

async def scrape_academic_one(isbn: str) -> Dict[str, Any]:
    """
    Scrapea un producto de global.oup.com/academic.

    Selectores exactos (DevTools):
      Título:  <h1 itemprop="name" class="product_biblio_title">
      Precio:  <span itemprop="price">7.99</span>
      Moneda:  <span id="structured-data-currency" itemprop="priceCurrency" content="GBP">
      ISBN:    <p>ISBN: 9780199537006</p>  en div.content_right.product_sidebar
    """
    global _context
    isbn = clean_isbn(isbn)
    search_url = build_url(isbn)

    if not (10 <= len(isbn) <= 13) or not isbn.isdigit():
        return {
            "isbn": isbn, "title": None, "price": None,
            "currency": None, "url": search_url,
            "error": "ISBN inválido (debe tener 10-13 dígitos numéricos)",
        }

    async with sem:
        page = await _context.new_page()
        try:
            # ── 1. Navegar al buscador de OUP con el ISBN ────────────────
            await page.goto(search_url, wait_until="domcontentloaded", timeout=90_000)

            # ── 2. Aceptar cookies ANTES de cualquier otra cosa ──────────
            await accept_cookies(page)

            # ── 3. Esperar a que aparezca el h1 del producto ─────────────
            product_loaded = False
            try:
                await page.locator("h1.product_biblio_title").wait_for(timeout=15_000)
                product_loaded = True
            except Exception:
                pass

            # Si no cargó, intentar clic en el primer resultado de búsqueda
            if not product_loaded:
                try:
                    link = page.locator(f"a[href*='{isbn}']").first
                    if await link.count() > 0:
                        await link.click(timeout=10_000)
                        await page.wait_for_load_state("domcontentloaded")
                        await accept_cookies(page)
                        try:
                            await page.locator("h1.product_biblio_title").wait_for(timeout=15_000)
                            product_loaded = True
                        except Exception:
                            pass
                except Exception:
                    pass

            # Dar tiempo extra para renderizado completo
            await page.wait_for_timeout(2000)

            # ── 4. Verificar que NO redirigió a Amazon ───────────────────
            current_url = page.url
            if "amazon" in current_url:
                return {
                    "isbn": isbn, "title": None, "price": None,
                    "currency": None, "url": current_url,
                    "error": "Redirigió a Amazon en vez de OUP",
                }

            # ── 5. Detectar "no encontrado" ──────────────────────────────
            nf = page.locator("text=/not found|page not found|no results found/i")
            if await nf.count() > 0:
                return {
                    "isbn": isbn, "title": None, "price": None,
                    "currency": None, "url": page.url,
                    "error": "Producto no encontrado para este ISBN",
                }

            # ═════════════════════════════════════════════════════════════
            # EXTRACCIÓN — Selectores exactos de DevTools
            # ═════════════════════════════════════════════════════════════

            # ── TÍTULO ───────────────────────────────────────────────────
            # <h1 itemprop="name" class="product_biblio_title">North and South</h1>
            title = None
            try:
                h1 = page.locator("h1.product_biblio_title").first
                title = (await h1.inner_text(timeout=5_000)).strip()
            except Exception:
                try:
                    h1 = page.locator('h1[itemprop="name"]').first
                    title = (await h1.inner_text(timeout=5_000)).strip()
                except Exception:
                    pass

            # ── PRECIO ───────────────────────────────────────────────────
            # <span itemprop="price">7.99</span>
            price = None

            try:
                price_span = page.locator('span[itemprop="price"]').first
                await price_span.wait_for(timeout=10_000)
                raw = (await price_span.inner_text(timeout=5_000)).strip()
                if raw:
                    price = raw.replace(",", ".")
            except Exception:
                pass

            if not price:
                try:
                    pp = page.locator("p.product_price").first
                    raw = (await pp.inner_text(timeout=5_000)).strip()
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
            # <span id="structured-data-currency" itemprop="priceCurrency" content="GBP">
            currency = None

            try:
                cs = page.locator('span[itemprop="priceCurrency"]').first
                code = await cs.get_attribute("content", timeout=3_000)
                if code:
                    currency = {"GBP": "£", "USD": "$", "EUR": "€"}.get(
                        code.strip(), code.strip()
                    )
            except Exception:
                pass

            if not currency:
                try:
                    strong = page.locator('strong[itemprop="offers"]').first
                    txt = (await strong.inner_text(timeout=3_000)).strip()
                    currency = extract_currency(txt)
                except Exception:
                    pass

            if not currency:
                try:
                    pp = page.locator("p.product_price").first
                    txt = (await pp.inner_text(timeout=3_000)).strip()
                    currency = extract_currency(txt)
                except Exception:
                    pass

            # ── ISBN (desde la página) ───────────────────────────────────
            # <p>ISBN: 9780199537006</p>
            page_isbn = isbn
            try:
                sidebar_ps = page.locator("div.content_right.product_sidebar p")
                count = await sidebar_ps.count()
                for i in range(count):
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

            # ── Resultado ────────────────────────────────────────────────
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
                "currency": None, "url": search_url,
                "error": f"Timeout ({isbn})",
            }
        except Exception as e:
            return {
                "isbn": isbn, "title": None, "price": None,
                "currency": None, "url": search_url,
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
    isbn_test = "9780199537006"
    result = await scrape_academic_one(isbn_test)
    return {"isbn_test": isbn_test, "result": result}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/version")
async def version():
    return {"version": "2.0.0", "source": "oup_academic"}


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
                "currency": None, "url": build_url(isbn), "error": str(r),
            })
        else:
            final.append(r)

    return {"source": "oup_academic", "count": len(final), "results": final}