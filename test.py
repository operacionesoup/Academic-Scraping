# oup_academic_server.py
# ─────────────────────────────────────────────────────────────────────────────
# FastAPI + Playwright — Scraper de precios Oxford University Press (Academic)
#
# Estrategia:
#   1. Navega a https://global.oup.com/academic/?lang=en&cc=es
#   2. Acepta cookies
#   3. Escribe el ISBN en el buscador  input[name="q"]
#   4. Hace clic en el botón Search    input#tab_search_submit
#   5. En los resultados, clic en el enlace del producto
#   6. Extrae título, precio, moneda e ISBN de la página de producto
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
    version="3.0.0",
)

# ─── Estado global del navegador ─────────────────────────────────────────────
_pw      = None
_browser = None
_context = None

# Máximo 2 páginas simultáneas para no saturar
sem = asyncio.Semaphore(2)

# URL base de OUP Academic
BASE_URL = "https://global.oup.com/academic/?lang=en&cc=es"


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

async def accept_cookies(page) -> None:
    """
    Cierra el banner de cookies de OUP Academic.
    Botón: "Aceptar todas las cookies"
    """
    for _ in range(5):
        try:
            btn = page.locator("button:has-text('Aceptar todas las cookies')").first
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
            btn = page.locator("#onetrust-accept-btn-handler").first
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
    Scrapea un producto de global.oup.com/academic usando el buscador.
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
        try:
            # ═════════════════════════════════════════════════════════════
            # PASO 1: Navegar a la página principal de OUP Academic
            # ═════════════════════════════════════════════════════════════
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(2000)

            # ═════════════════════════════════════════════════════════════
            # PASO 2: Aceptar cookies
            # ═════════════════════════════════════════════════════════════
            await accept_cookies(page)

            # ═════════════════════════════════════════════════════════════
            # PASO 3: Escribir el ISBN en el buscador
            # DevTools: <input name="q" type="text" class="default_text">
            # ═════════════════════════════════════════════════════════════
            search_input = page.locator('input[name="q"]').first
            await search_input.wait_for(timeout=10_000)
            # Limpiar el campo (puede tener placeholder text)
            await search_input.click()
            await search_input.fill("")
            await search_input.fill(isbn)
            await page.wait_for_timeout(500)

            # ═════════════════════════════════════════════════════════════
            # PASO 4: Hacer clic en el botón Search
            # DevTools: <input id="tab_search_submit" class="simple_search_submit"
            #            type="submit" value="Search">
            # ═════════════════════════════════════════════════════════════
            search_btn = page.locator("input#tab_search_submit").first
            await search_btn.click(timeout=5_000)

            # Esperar a que cargue la página de resultados o producto
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(3000)

            # Aceptar cookies de nuevo si aparecen tras la navegación
            await accept_cookies(page)

            # ═════════════════════════════════════════════════════════════
            # PASO 5: Verificar si estamos en la página de producto
            #         o en la lista de resultados de búsqueda
            # ═════════════════════════════════════════════════════════════
            product_loaded = False

            # Verificar si ya estamos en la página de producto
            try:
                h1 = page.locator("h1.product_biblio_title").first
                if await h1.count() > 0:
                    product_loaded = True
            except Exception:
                pass

            # Si no estamos en el producto, buscar el enlace en resultados
            if not product_loaded:
                try:
                    # Buscar enlace que contenga el ISBN en su href
                    result_link = page.locator(f"a[href*='{isbn}']").first
                    if await result_link.count() > 0:
                        await result_link.click(timeout=10_000)
                        await page.wait_for_load_state("domcontentloaded")
                        await page.wait_for_timeout(3000)
                        await accept_cookies(page)
                        product_loaded = True
                except Exception:
                    pass

            # Si sigue sin cargar, intentar con un enlace de producto genérico
            if not product_loaded:
                try:
                    result_link = page.locator("a[href*='/academic/product/']").first
                    if await result_link.count() > 0:
                        await result_link.click(timeout=10_000)
                        await page.wait_for_load_state("domcontentloaded")
                        await page.wait_for_timeout(3000)
                        await accept_cookies(page)
                        product_loaded = True
                except Exception:
                    pass

            # ── Verificar que NO estamos en Amazon ───────────────────────
            if "amazon" in page.url:
                return {
                    "isbn": isbn, "title": None, "price": None,
                    "currency": None, "url": page.url,
                    "error": "Redirigió a Amazon",
                }

            # ── Verificar que encontramos algo ───────────────────────────
            if not product_loaded:
                # Último intento: verificar si h1 existe de todas formas
                try:
                    h1 = page.locator("h1.product_biblio_title").first
                    await h1.wait_for(timeout=5_000)
                    product_loaded = True
                except Exception:
                    return {
                        "isbn": isbn, "title": None, "price": None,
                        "currency": None, "url": page.url,
                        "error": "Producto no encontrado para este ISBN",
                    }

            # ═════════════════════════════════════════════════════════════
            # PASO 6: EXTRACCIÓN DE DATOS — Selectores exactos DevTools
            # ═════════════════════════════════════════════════════════════

            # ── TÍTULO ───────────────────────────────────────────────────
            # <h1 itemprop="name" class="product_biblio_title">
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
                ps = page.locator('span[itemprop="price"]').first
                await ps.wait_for(timeout=10_000)
                raw = (await ps.inner_text(timeout=5_000)).strip()
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
    return {"version": "3.0.0", "source": "oup_academic"}


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
