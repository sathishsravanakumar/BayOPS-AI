import asyncio
import base64
import os
import re
import sys
import threading
from typing import Callable, Awaitable

import anthropic
from playwright.async_api import async_playwright

from schemas import MechanicIntent

# Tracks open Playwright (pw, browser) per bay so we can close on bay clear.
_open_browsers: dict[str, tuple] = {}


def _close_browser_sync(bay_id: str):
    """Run in a daemon thread: close Playwright browser for the given bay."""
    entry = _open_browsers.pop(bay_id, None)
    if not entry:
        return
    pw, browser = entry
    # See start_browser_thread() in main.py for why this matters on Windows.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    try:
        async def _do():
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass
        loop.run_until_complete(_do())
    finally:
        loop.close()


def cleanup_bay_browser(bay_id: str):
    """Non-blocking entry point: spawns a thread to close any open browser for this bay."""
    if bay_id in _open_browsers:
        threading.Thread(target=_close_browser_sync, args=(bay_id,), daemon=True).start()

SCREEN_WIDTH = 1366
SCREEN_HEIGHT = 768
COMPUTER_USE_BETA = "computer-use-2025-01-24"
MAX_STEPS = 50


async def take_screenshot(page) -> str:
    """Take a screenshot and return base64-encoded PNG."""
    png_bytes = await page.screenshot()
    return base64.standard_b64encode(png_bytes).decode()


async def execute_action(page, action: dict):
    """Execute a computer-use action on the Playwright page."""
    action_type = action.get("action")

    if action_type == "screenshot":
        pass  # Handled in the main loop

    elif action_type == "left_click":
        x, y = action["coordinate"]
        await page.mouse.click(x, y)
        await asyncio.sleep(0.4)

    elif action_type == "right_click":
        x, y = action["coordinate"]
        await page.mouse.click(x, y, button="right")
        await asyncio.sleep(0.3)

    elif action_type == "double_click":
        x, y = action["coordinate"]
        await page.mouse.dblclick(x, y)
        await asyncio.sleep(0.4)

    elif action_type == "middle_click":
        x, y = action["coordinate"]
        await page.mouse.click(x, y, button="middle")
        await asyncio.sleep(0.3)

    elif action_type == "type":
        await page.keyboard.type(action["text"], delay=30)
        await asyncio.sleep(0.3)

    elif action_type == "key":
        key = action["key"]
        # Map common key names
        key_map = {"Return": "Enter", "ctrl+l": "Control+l", "ctrl+a": "Control+a"}
        await page.keyboard.press(key_map.get(key, key))
        await asyncio.sleep(0.3)

    elif action_type == "scroll":
        x, y = action["coordinate"]
        delta_x = action.get("delta_x", 0)
        delta_y = action.get("delta_y", 0)
        await page.mouse.move(x, y)
        await page.mouse.wheel(delta_x, delta_y)
        await asyncio.sleep(0.3)

    elif action_type == "mouse_move":
        x, y = action["coordinate"]
        await page.mouse.move(x, y)
        await asyncio.sleep(0.1)

    elif action_type in ("left_click_drag", "right_click_drag"):
        start_x, start_y = action["start_coordinate"]
        end_x, end_y = action["coordinate"]
        btn = "right" if action_type == "right_click_drag" else "left"
        await page.mouse.move(start_x, start_y)
        await page.mouse.down(button=btn)
        await asyncio.sleep(0.1)
        await page.mouse.move(end_x, end_y)
        await asyncio.sleep(0.1)
        await page.mouse.up(button=btn)
        await asyncio.sleep(0.3)


async def scrape_cart(page) -> dict:
    """Scrape product info and total from the cart page.
    Tries CSS selectors first; falls back to text parsing."""
    await asyncio.sleep(2)

    cart_items = []
    cart_total = 0.0

    # Primary: structured selector scan
    try:
        for item_sel in [
            '[data-testid*="cart-item"]', '[class*="cartItem"]',
            '[class*="cart-line"]', '[class*="cart-product"]', '.cart-item',
        ]:
            items_loc = page.locator(item_sel)
            n = await items_loc.count()
            if n > 0:
                for i in range(n):
                    el = items_loc.nth(i)
                    txt = (await el.text_content() or "").strip()
                    pn = re.search(r'Part\s*#?\s*:?\s*([A-Z0-9\-]+)', txt, re.IGNORECASE)
                    part_number = pn.group(1) if pn else "N/A"
                    prices = [float(m) for m in re.findall(r'\$(\d+\.\d{2})', txt)
                              if 0.5 <= float(m) <= 9999]
                    price = min(prices) if prices else 0.0
                    lines_clean = [l.strip() for l in txt.split('\n') if len(l.strip()) > 5
                                   and not l.strip().startswith('$')]
                    name = lines_clean[0] if lines_clean else ""
                    cart_items.append({"name": name, "part_number": part_number, "price": price})
                break
    except Exception:
        pass

    # Fallback: text-based parsing (original approach)
    if not cart_items:
        try:
            body = await page.inner_text("body")
            lines = [l.strip() for l in body.split("\n") if l.strip()]
            for i, line in enumerate(lines):
                if line.startswith("Part #") or line.startswith("Part#"):
                    part_number = line.replace("Part #", "").replace("Part#", "").strip()
                    name = ""
                    for j in range(i - 1, max(0, i - 6), -1):
                        c = lines[j]
                        if len(c) > 8 and not c.startswith("$") and "Pickup" not in c \
                                and "Delivery" not in c and "stock" not in c.lower():
                            name = c
                            break
                    price = 0.0
                    for j in range(max(0, i - 4), min(len(lines), i + 4)):
                        m = re.search(r"\$(\d+\.?\d*)", lines[j])
                        if m:
                            price = float(m.group(1))
                            break
                    if part_number:
                        cart_items.append({"name": name, "part_number": part_number, "price": price})
        except Exception:
            pass

    # Cart total — selector first, then text
    try:
        for sel in ['[data-testid*="subtotal"]', '[data-testid*="order-total"]',
                    '[class*="subtotal"]', '[class*="orderTotal"]']:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                text = await el.text_content() or ""
                m = re.search(r'\$(\d+[,\d]*\.?\d*)', text)
                if m:
                    cart_total = float(m.group(1).replace(",", ""))
                    break
    except Exception:
        pass

    if not cart_total:
        try:
            body = await page.inner_text("body")
            for line in body.split("\n"):
                if "subtotal" in line.lower():
                    m = re.search(r"\$(\d+[,\d]*\.?\d*)", line)
                    if m:
                        cart_total = float(m.group(1).replace(",", ""))
                        break
        except Exception:
            pass

    if not cart_total and cart_items:
        cart_total = sum(it["price"] for it in cart_items)

    return {"cart_items": cart_items, "cart_total": cart_total}


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


VENDOR_SEARCH_URLS = {
    "AutoZone": "https://www.autozone.com/searchresult?searchText={query}",
    "NAPA Auto Parts": "https://www.napaonline.com/en/search?q={query}",
    "Advance Auto Parts": "https://shop.advanceautoparts.com/find/{query}",
}

VENDOR_CART_URLS = {
    "AutoZone": "https://www.autozone.com/cart",
    "NAPA Auto Parts": "https://www.napaonline.com/en/cart",
    "Advance Auto Parts": "https://cart.advanceautoparts.com/web/cart",
}


async def _scrape_price_from_page(page) -> float:
    """Extract the main product price from the current page."""

    # 1. Try specific price CSS selectors (most reliable on product pages)
    price_selectors = [
        '[data-testid*="price"]:not([data-testid*="was"]):not([data-testid*="original"])',
        '.product-price .price',
        '.prod-sale-price',
        '.sale-price',
        '[itemprop="price"]',
        '[class*="currentPrice"]',
        '[class*="salePrice"]',
        '[class*="finalPrice"]',
        '[class*="product-price"]',
        '.price-box .price',
        '.pricebox .price',
    ]
    for sel in price_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                text = await el.text_content() or ""
                m = re.search(r'\$(\d+\.\d{2})', text)
                if m:
                    price = float(m.group(1))
                    if 1.0 <= price <= 9999.0:
                        return price
        except Exception:
            pass

    # 2. JS: find the price closest to an "Add to Cart" button
    try:
        price = await page.evaluate("""() => {
            // Look for elements with "$XX.XX" near add-to-cart buttons
            const addBtns = [...document.querySelectorAll('button')]
                .filter(b => /add.*cart/i.test(b.textContent));
            for (const btn of addBtns) {
                // Walk up to find a price nearby
                let el = btn.parentElement;
                for (let i = 0; i < 8 && el; i++) {
                    const text = el.textContent || '';
                    const m = text.match(/\\$(\\d{1,4}\\.\\d{2})/);
                    if (m) {
                        const p = parseFloat(m[1]);
                        if (p >= 1 && p <= 9999) return p;
                    }
                    el = el.parentElement;
                }
            }
            // Last resort: first visible dollar price in page body
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if (el.children.length > 0) continue;  // only leaf text nodes
                const t = (el.textContent || '').trim();
                const m = t.match(/^\\$(\\d{1,4}\\.\\d{2})$/);
                if (m) {
                    const p = parseFloat(m[1]);
                    if (p >= 1 && p <= 999) return p;
                }
            }
            return 0;
        }""")
        if price and float(price) > 0:
            return float(price)
    except Exception:
        pass

    # 3. Regex fallback on full page text
    try:
        text = await page.inner_text("body")
        matches = re.findall(r'\$(\d{1,4}\.\d{2})', text)
        valid = [float(m) for m in matches if 1.0 <= float(m) <= 999.0]
        if valid:
            # Return the most common price (product pages repeat their price)
            from collections import Counter
            return float(Counter(valid).most_common(1)[0][0])
    except Exception:
        pass

    return 0.0


async def _navigate_to_product(page, url: str) -> float:
    """Navigate to vendor URL, handle listing pages, return scraped price."""
    try:
        await page.goto(url, timeout=25000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(3)

        # Dismiss cookie banners
        for sel in ['#onetrust-accept-btn-handler', 'button:has-text("Accept All")',
                    'button:has-text("Got it")', 'button:has-text("Accept Cookies")']:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(1)
                    break
            except Exception:
                pass

        # If listing/search page, click first product link
        for sel in ['a[href*="/p/"]', 'a[href*="/product/"]', 'a[href*="/item/"]',
                    '.product-title a', '[data-testid*="product"] a', '.product-name a']:
            try:
                link = page.locator(sel).first
                if await link.is_visible(timeout=3000):
                    await link.click()
                    await asyncio.sleep(4)
                    break
            except Exception:
                pass

        price = await _scrape_price_from_page(page)
        return price
    except Exception:
        return 0.0


async def _add_to_cart_on_page(page) -> bool:
    """Click 'Add to Cart' button on the current product page."""
    cart_selectors = [
        'button:has-text("Add to Cart")',
        'button:has-text("Add TO CART")',
        'button:has-text("Cart")',
        '[data-testid*="add-to-cart"]',
        'button[id*="addToCart"]',
        '.add-to-cart',
    ]
    for sel in cart_selectors:
        try:
            btn = page.locator(sel).first
            await btn.wait_for(state="visible", timeout=5000)
            await btn.click()
            await asyncio.sleep(2)
            return True
        except Exception:
            pass
    return False


async def _playwright_checkout(
    intent: MechanicIntent,
    search_results: dict,
    log_callback: Callable[[str], Awaitable[None]],
    bay_id: str = "",
) -> dict:
    """Open tabs for all vendors, compare real prices, add cheapest to cart."""

    vehicle = intent.vehicle
    v_str = f"{vehicle.year} {vehicle.make} {vehicle.model}"
    parts = search_results.get("results", [])

    await log_callback(f"Opening browser — comparing vendors for {v_str}...")

    _headless = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=_headless,
        args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
    )
    if bay_id:
        _open_browsers[bay_id] = (pw, browser)
    context = await browser.new_context(
        user_agent=UA,
        viewport={"width": 1366, "height": 768},
        locale="en-US",
    )
    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => false });")

    cart_results = []   # {description, vendor, price, page_index}
    pages = {}          # vendor -> page (kept open so we can add to cart)
    all_vendor_prices = {}  # description -> [{vendor, price}]

    for part in parts:
        desc = part.get("description", "unknown")
        vendor_options = part.get("vendor_options", [])
        await log_callback(f"\nComparing vendors for: {desc}")

        # Collect vendor URLs (from search results or build search URL)
        vendor_urls = {}
        for opt in vendor_options:
            vname = opt.get("vendor", "")
            url = opt.get("source_url", "")
            if url and vname:
                vendor_urls[vname] = url

        # Fill in any missing vendors with search URLs
        for vname, url_tpl in VENDOR_SEARCH_URLS.items():
            if vname not in vendor_urls:
                query = f"{vehicle.year} {vehicle.make} {vehicle.model} {desc}".replace(" ", "+")
                vendor_urls[vname] = url_tpl.format(query=query)

        # Open a tab for each vendor and scrape price
        prices_found = []
        desc_pages = {}

        for vname, url in vendor_urls.items():
            await log_callback(f"  Checking {vname}...")
            pg = await context.new_page()
            desc_pages[vname] = pg
            price = await _navigate_to_product(pg, url)
            await log_callback(f"    {vname}: ${price:.2f}" if price > 0 else f"    {vname}: price not found")
            if price > 0:
                prices_found.append({"vendor": vname, "price": price, "page": pg})

        all_vendor_prices[desc] = [{"vendor": v, "price": p} for v, p in
                                    [(x["vendor"], x["price"]) for x in prices_found]]

        if not prices_found:
            await log_callback(f"  No prices found for {desc} — skipping cart add")
            # Close extra pages
            for pg in desc_pages.values():
                try:
                    await pg.close()
                except Exception:
                    pass
            continue

        # Pick cheapest
        cheapest = min(prices_found, key=lambda x: x["price"])
        await log_callback(f"  Cheapest: {cheapest['vendor']} at ${cheapest['price']:.2f}")

        # Close tabs for vendors we're NOT using
        for vname, pg in desc_pages.items():
            if vname != cheapest["vendor"]:
                try:
                    await pg.close()
                except Exception:
                    pass

        # Add to cart on the cheapest vendor's tab
        cart_page = cheapest["page"]
        await log_callback(f"  Adding to cart on {cheapest['vendor']}...")
        added = await _add_to_cart_on_page(cart_page)

        if added:
            await log_callback(f"  Added to cart: {desc} from {cheapest['vendor']} at ${cheapest['price']:.2f}")
            cart_results.append({
                "description": desc,
                "vendor": cheapest["vendor"],
                "price": cheapest["price"],
            })
            pages[cheapest["vendor"]] = cart_page
        else:
            await log_callback(f"  Could not click Add to Cart on {cheapest['vendor']}")
            try:
                await cart_page.close()
            except Exception:
                pass

    # Navigate to each vendor's cart and aggregate items/totals
    await log_callback("\nVerifying cart contents...")
    all_cart_items: list[dict] = []
    all_cart_total = 0.0

    for vendor, pg in pages.items():
        cart_url = VENDOR_CART_URLS.get(vendor, "https://www.autozone.com/cart")
        try:
            await pg.goto(cart_url, timeout=20000)
            try:
                await pg.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            v_cart = await scrape_cart(pg)
            all_cart_items.extend(v_cart["cart_items"])
            all_cart_total += v_cart["cart_total"]
            await log_callback(
                f"Cart verified on {vendor}: {len(v_cart['cart_items'])} item(s), ${v_cart['cart_total']:.2f}"
            )
        except Exception as e:
            await log_callback(f"Cart error on {vendor}: {str(e)[:60]}")

    cart_data = {"cart_items": all_cart_items, "cart_total": all_cart_total}

    # Fall back to click-tracking summary if scrape came up empty
    if not cart_data["cart_items"] and cart_results:
        total = sum(r["price"] for r in cart_results)
        cart_data = {
            "cart_items": [{"name": r["description"], "part_number": "N/A", "price": r["price"], "vendor": r["vendor"]} for r in cart_results],
            "cart_total": total,
        }

    if cart_results:
        await log_callback(f"\nSummary: {len(cart_results)} part(s) added")
        for r in cart_results:
            await log_callback(f"  {r['description']}: {r['vendor']} ${r['price']:.2f}")
        await log_callback(f"  Cart total: ${cart_data['cart_total']:.2f}")

    return {
        "status": "checkout_ready",
        "items_added": [r["description"] for r in cart_results],
        "cart_items": cart_data["cart_items"],
        "cart_total": cart_data["cart_total"],
        "vendor_prices": all_vendor_prices,
        "message": f"Compared vendors, added cheapest for {len(cart_results)} part(s) — total ${cart_data['cart_total']:.2f}",
    }


async def run_browser_checkout(
    intent: MechanicIntent,
    search_results: dict,
    log_callback: Callable[[str], Awaitable[None]],
    bay_id: str = "",
) -> dict:
    """Use Claude Computer Use to navigate and add to cart; falls back to Playwright."""

    vehicle = intent.vehicle
    v_str = f"{vehicle.year} {vehicle.make} {vehicle.model}"
    parts = search_results.get("results", [])

    if not parts:
        await log_callback("No parts to add to cart.")
        return {"status": "no_parts", "cart_items": [], "cart_total": 0.0}

    await log_callback(f"Launching Claude Computer Use for {v_str}...")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        await log_callback("ANTHROPIC_API_KEY not set — using Playwright fallback.")
        return await _playwright_checkout(intent, search_results, log_callback, bay_id)

    _headless = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=_headless,
        args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
    )
    page = await browser.new_page(
        viewport={"width": SCREEN_WIDTH, "height": SCREEN_HEIGHT},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => false });")
    if bay_id:
        _open_browsers[bay_id] = (pw, browser)

    # Start at Google
    await page.goto("https://www.google.com", wait_until="domcontentloaded")
    await asyncio.sleep(2)

    client = anthropic.Anthropic(api_key=api_key)

    # Build task prompt
    part_lines = "\n".join(f"- {p.get('description', 'unknown')}" for p in parts)

    task = (
        f"You are controlling a real web browser. Your goal is to find auto parts on AutoZone "
        f"and add them to the cart. Here is your step-by-step process:\n\n"
        f"VEHICLE: {v_str}\n\n"
        f"PARTS TO ADD TO CART:\n{part_lines}\n\n"
        f"STEPS FOR EACH PART:\n"
        f"1. Go to https://www.google.com\n"
        f"2. In the Google search bar, type: [part name] {v_str} AutoZone\n"
        f"   Example: 'front brake pads {v_str} AutoZone'\n"
        f"3. Press Enter to search\n"
        f"4. Look at the search results and click on an AutoZone.com link\n"
        f"5. On the AutoZone page, find the right product for the {v_str}\n"
        f"6. If it's a category/listing page, click on the specific product\n"
        f"7. On the product page, click 'Add to Cart' or 'Add TO CART'\n"
        f"8. Repeat steps 1-7 for each part\n\n"
        f"AFTER ALL PARTS ARE ADDED:\n"
        f"- Go to https://www.autozone.com/cart\n"
        f"- Once the cart page is visible, STOP — do not click Checkout\n\n"
        f"IMPORTANT TIPS:\n"
        f"- Dismiss any cookie banners or popups by clicking Accept or Close\n"
        f"- If a page is blocked or restricted, go back to Google and try a different link\n"
        f"- If you can't find a product on AutoZone, search Google again with different keywords\n"
        f"- When on a search results page, scroll down to see more results if needed\n"
        f"- Always verify the part fits the {v_str} before adding to cart\n\n"
        f"Start by taking a screenshot to see what's currently on screen."
    )

    messages = [{"role": "user", "content": task}]
    cart_reached = False

    for step in range(MAX_STEPS):
        # Take screenshot at start of each step
        screenshot_b64 = await take_screenshot(page)

        # If the last message is a tool_result placeholder, fill it in with the screenshot
        if (messages and messages[-1]["role"] == "user"
                and isinstance(messages[-1]["content"], list)
                and messages[-1]["content"]
                and messages[-1]["content"][-1].get("type") == "tool_result"):
            # Update the last tool result to include the screenshot
            messages[-1]["content"][-1]["content"] = [{
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64},
            }]
        elif messages[-1]["role"] == "assistant":
            # Fresh screenshot after assistant action — add as new user message
            # But we actually need to attach this to the pending tool_result
            pass

        # Call Claude Computer Use
        try:
            response = client.beta.messages.create(
                model="claude-opus-4-8",
                max_tokens=4096,
                tools=[{
                    "type": "computer_20250124",
                    "name": "computer",
                    "display_width_px": SCREEN_WIDTH,
                    "display_height_px": SCREEN_HEIGHT,
                }],
                messages=messages,
                betas=[COMPUTER_USE_BETA],
            )
        except Exception as e:
            await log_callback(f"Computer Use API error: {str(e)[:120]}")
            break

        # Append assistant response
        messages.append({"role": "assistant", "content": response.content})

        # Log any text Claude says
        for block in response.content:
            if hasattr(block, "text") and block.text:
                await log_callback(f"Agent: {block.text[:150]}")

        # Check if done
        if response.stop_reason == "end_turn":
            await log_callback("Agent completed task.")
            break

        # Find tool_use block
        tool_use = None
        for block in response.content:
            if block.type == "tool_use":
                tool_use = block
                break

        if tool_use is None:
            break

        action = tool_use.input
        action_name = action.get("action", "unknown")
        await log_callback(f"Step {step + 1}: {action_name}")

        # Execute the action
        error_msg = None
        try:
            await execute_action(page, action)
        except Exception as e:
            error_msg = str(e)[:80]
            await log_callback(f"Action error: {error_msg}")

        # Take new screenshot after action
        await asyncio.sleep(0.5)
        new_screenshot = await take_screenshot(page)

        # Build tool result with screenshot
        tool_result_content = [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": new_screenshot},
        }]

        tool_result = {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": tool_result_content,
        }
        if error_msg:
            tool_result["is_error"] = True

        messages.append({"role": "user", "content": [tool_result]})

        # Check if on cart page
        current_url = page.url
        if "autozone.com/cart" in current_url:
            await log_callback("Cart page reached!")
            cart_reached = True
            await asyncio.sleep(3)
            break

    # Scrape cart for real prices
    cart_data = {"cart_items": [], "cart_total": 0.0}
    if cart_reached or "autozone.com/cart" in page.url:
        cart_data = await scrape_cart(page)
        await log_callback(f"Cart: {len(cart_data['cart_items'])} item(s), total ${cart_data['cart_total']:.2f}")
        for it in cart_data["cart_items"]:
            await log_callback(f"  {it['name']} (#{it['part_number']}) — ${it['price']:.2f}")

    await log_callback("Browser stays open for you to review the cart.")

    return {
        "status": "checkout_ready",
        "cart_items": cart_data["cart_items"],
        "cart_total": cart_data["cart_total"],
        "message": f"Computer Use agent completed for {v_str}",
    }