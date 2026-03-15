# ─────────────────────────────────────────────────────────────────────────────
# FastAPI + Playwright — Scraper de precios Oxford University Press (Academic)
# Arranque:
#   uvicorn oup_academic_server:app --reload --port 8003
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import re
import asyncio
from typing import Optional, List, Dict, Any

app = FastAPI(
    title="Oxford University Press — Academic Price Scraper",
    version="5.0.0",
)

_pw      = None
_browser = None
_context = None
sem      = asyncio.Semaphore(3)


def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "").replace("-", "")


def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d{2,3})?)", text)
    return m.group(1).replace(",", ".") if m else None


def extract_currency(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"([£$€])", text)
    return m.group(1) if m else None


# ─── Lifecycle ───────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global _pw, _browser, _context
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
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
    try:
        if _context: await _context.close()
        if _browser: await _browser.close()
        if _pw:      await _pw.stop()
    except Exception:
        pass


# ─── Cookies ─────────────────────────────────────────────────────────────────

async def accept_cookies(page) -> None:
    """
    <button id="onetrust-accept-btn-handler">Accept all cookies</button>
    """
    try:
        btn = page.locator("#onetrust-accept-btn-handler")
        await btn.wait_for(state="visible", timeout=5_000)
        await btn.click(timeout=3_000)
        await page.wait_for_timeout(500)
    except Exception:
        pass


# ─── Scraping core ───────────────────────────────────────────────────────────

async def scrape_academic_one(isbn: str) -> Dict[str, Any]:
    global _context
    isbn = clean_isbn(isbn)

    if not (10 <= len(isbn) <= 13) or not isbn.isdigit():
        return {
            "isbn": isbn, "title": None, "price": None,
            "currency": None, "url": "",
            "error": "ISBN inválido (debe tener 10-13 dígitos numéricos)",
        }

    search_url = f"https://global.oup.com/academic/search?q={isbn}&cc=es&lang=en"

    async with sem:
        page = await _context.new_page()
        try:
            # ── 1. Navegar a la búsqueda directa ────────────────────────
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)

            # ── 2. Aceptar cookies ───────────────────────────────────────
            await accept_cookies(page)

            # ── 3. Esperar a que cargue el contenido ─────────────────────
            #    Esperar a que el DOM se estabilice
            await page.wait_for_timeout(2000)

            # ── 4. Verificar si estamos en la página de producto ─────────
            product_loaded = False
            try:
                h1 = page.locator("h1.product_biblio_title").first
                await h1.wait_for(timeout=8_000)
                product_loaded = True
            except Exception:
                pass

            # ── 5. Si no, buscar enlace al producto en resultados ────────
            if not product_loaded:
                try:
                    link = page.locator(f"a[href*='{isbn}']").first
                    if await link.count() > 0:
                        await link.click(timeout=10_000)
                        await page.wait_for_load_state("domcontentloaded")
                        await accept_cookies(page)
                        await page.wait_for_timeout(2000)
                        try:
                            await page.locator("h1.product_biblio_title").first.wait_for(timeout=8_000)
                            product_loaded = True
                        except Exception:
                            pass
                except Exception:
                    pass

            if not product_loaded:
                try:
                    link = page.locator("a[href*='/academic/product/']").first
                    if await link.count() > 0:
                        await link.click(timeout=10_000)
                        await page.wait_for_load_state("domcontentloaded")
                        await accept_cookies(page)
                        await page.wait_for_timeout(2000)
                        try:
                            await page.locator("h1.product_biblio_title").first.wait_for(timeout=8_000)
                            product_loaded = True
                        except Exception:
                            pass
                except Exception:
                    pass

            # ── Verificar Amazon redirect ────────────────────────────────
            if "amazon" in page.url:
                return {
                    "isbn": isbn, "title": None, "price": None,
                    "currency": None, "url": page.url,
                    "error": "Redirigió a Amazon",
                }

            if not product_loaded:
                return {
                    "isbn": isbn, "title": None, "price": None,
                    "currency": None, "url": page.url,
                    "error": "Producto no encontrado para este ISBN",
                }

            # ═════════════════════════════════════════════════════════════
            # EXTRACCIÓN — Esperar explícitamente a cada elemento
            # ═════════════════════════════════════════════════════════════

            # ── TÍTULO ───────────────────────────────────────────────────
            # <h1 itemprop="name" class="product_biblio_title">
            title = None
            try:
                h1 = page.locator("h1.product_biblio_title").first
                await h1.wait_for(timeout=5_000)
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

            # Fallback: p.product_price
            if not price:
                try:
                    pp = page.locator("p.product_price").first
                    await pp.wait_for(timeout=5_000)
                    raw = (await pp.inner_text(timeout=5_000)).strip()
                    price = normalize_price(raw)
                except Exception:
                    pass

            # Fallback: regex en HTML
            if not price:
                try:
                    html = await page.content()
                    m = re.search(r'itemprop="price"[^>]*>(\d{1,4}[.,]\d{2})<', html)
                    if m:
                        price = m.group(1).replace(",", ".")
                except Exception:
                    pass

            # ── MONEDA ───────────────────────────────────────────────────
            # <span itemprop="priceCurrency" content="GBP">
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

            # ── ISBN ─────────────────────────────────────────────────────
            # <p>ISBN: 9780199537006</p>
            page_isbn = isbn
            try:
                sidebar_ps = page.locator("div.content_right.product_sidebar p")
                count = await sidebar_ps.count()
                for i in range(count):
                    try:
                        txt = (await sidebar_ps.nth(i).inner_text(timeout=1_000)).strip()
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
                "isbn": page_isbn, "title": title,
                "price": price, "currency": currency,
                "url": page.url, "error": None,
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
        "docs": "/docs", "health": "/health",
        "test": "/test",
        "example": "/oup/academic?isbn=9780199537006",
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
    return {"version": "5.0.0", "source": "oup_academic"}


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
                "currency": None, "url": "", "error": str(r),
            })
        else:
            final.append(r)

    return {"source": "oup_academic", "count": len(final), "results": final}