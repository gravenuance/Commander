# 0067: Cardmarket seller optimiser in the app

**Status:** Accepted · **Date:** 2026-11-09

## Context

The owner buys the Don't Have cards on Cardmarket. Cardmarket's Shopping Wizard picks sellers for a wants
list, but its picks are often worse than the owner's own method: start from the sellers who carry the most
wanted cards, or the rarest ones, at fair prices; pay a little more per card to save a seller's shipping
(50 cents more beats 3 € of shipping); end with a couple of stray sellers. The Wizard does not say what it
weighs, and it looks at a card's last 20 printings only.

Cardmarket's API takes no new applications, and its existing credentials may not be shared with
third-party software. The owner read Cardmarket's General Terms and Conditions on 2026-11-09: they cover
the API and commercial applications and say nothing explicit about a buyer's own browsing being automated.
The owner accepts the remaining risk to their account. The cart page warns that "abuse of the shopping cart
may result in account suspension". The open-source tools that do this (cw-wizard, CMBuyerStudio) store the
user's password in plain text, type it into the login form and fail on two-factor authentication.

A prototype, `tools/cardmarket_login.py`, probed every step against the live site on 2026-11-09, at about
one page every 4 seconds:

- **Sign-in.** The user signs in by hand in a WebView2 window (password, 2FA, Cloudflare's check). The session
  is the `PHPSESSID` session cookie, which WebView2 drops on close; the "stay logged in" tickbox only sets
  `cm_TD`, which skips 2FA on this device for 30 days. Saving `PHPSESSID` to Windows Credential Manager while
  signed in and adding it back before the next load kept the session across a restart. With WebView2's
  password autosave turned on, it offered to save the password, so a sign-in after the server ends the session
  is one click.
- **A card's offers.** `/en/Magic/Cards/<Card-Name>` lists offers across every printing, cheapest first, 50 to
  a page with a "Show more results" button. Each row (`#articleRow<idArticle>`) gives the seller, their
  country, rating, sales and items for sale, estimated days to the buyer, the expansion, condition, language,
  price, quantity, and the site's own "Put in shopping cart" form. The page filters by `sellerCountry`,
  `sellerType`, `language`, `minCondition`, `idExpansion`, `idRarity`, foil, signed, altered,
  `sellerReputation` and `maxShippingTime`.
- **A seller's stock of the wants.** `/en/Magic/Users/<seller>/Offers/Singles?idWantslist=<id>` lists the
  seller's offers of the list's cards only, every printing, as `#stockRow<idArticle>` rows. The wants list's
  own language and condition settings filter it: a seller of Japanese copies showed nothing for a list that
  wanted English.
- **Shipping.** Cardmarket sets shipping, not the seller: by origin and destination country, weight and size,
  plus a packaging fee. Its help page's calculator reads a public JSON endpoint,
  `help.cardmarket.com/api/shippingCosts?locale=en&fromCountry=<id>&toCountry=<id>`, which lists each method
  with its name, `isTracked`, `maxValue`, `maxWeight` and `price` (fee included); `/api/countries` gives the 35
  country ids (Sweden is 28). France to Sweden, "Lettre Internationale" up to 20 g, 2,55 €, matched the cart to
  the cent. Cardmarket's own rule of thumb gives the weight: up to 4 cards 20 g, 17 cards 50 g, 40 cards 100 g.
  Tracked shipping is mandatory over 25 €, and for some sellers lower (new sellers over 10 €, too many
  shipments on the way, a loss rate over 2 %), which only the cart shows.
- **Cart.** Clicking an offer's cart button added it (its request is under the decision's cart); the cart
  page (`/en/Magic/ShoppingCart`) shows each seller's items, article value, shipping method and cost, total
  and estimated arrival. "Remove articles from this seller" (`ShoppingCart_RemoveSeller`) and per-article
  remove (`ShoppingCart_RemoveArticle`) took it out again. The cart already held an item the owner had put
  there, which stayed untouched.
- **Wants lists.** `/en/Magic/Wants` creates a list (`wlName`, letters, digits, spaces and hyphens, 30 at most);
  `/en/Magic/Wants/<id>/AddDeckList` takes the text `cmdr toget --cardmarket` prints. Apostrophes and
  "Front // Back" names matched. Imported wants start at any printing, any language, minimum condition Poor.
  The list's bulk "Modify selected" form sets language (`idLanguages[]`, 1 is English) and minimum condition
  (`minCondition`, 2 is Near Mint) for every ticked want, but also sets each one's quantity to its own
  quantity field (empty means 1). A list can be deleted only when empty; one of each want's two delete buttons
  asks for confirmation in a browser dialog, the other (`#deleteWant<id>`) does not. Each list also offers
  Cardmarket's "Sellers with the Most Cards" table, for professional sellers only.
- **Pitfalls.** Cloudflare showed its check once in about 40 page loads and passed it by itself. A script that
  was killed left WebView2's browser process holding the profile, and the next window stayed white; closing
  the window ends it cleanly. A scripted step right after a click that loads a new page sometimes hung, so the
  scanner must wait for each navigation to finish.

## Options

- **Cardmarket's Shopping Wizard, fed by `cmdr toget --cardmarket` (what the owner did before).** Nothing to
  build or risk, but its picks are the problem.
- **An optimiser over pages the owner's own signed-in browser window reads (chosen).** It sees what the owner
  sees and can weigh shipping exactly; it depends on Cardmarket's undocumented page markup and acts on a real
  account.
- **A scraper with its own HTTP client and the owner's cookies, as cw-wizard does.** No browser window, but
  Cloudflare blocks clients that are not browsers, and copying a session out of a browser is what bot
  protection looks for.
- **A paid scraping service (Apify actors).** Prices without a login, but no wants lists, carts or the
  owner's own view, and it costs per request.

## Decision

The app gets a **Buy** screen that turns the Don't Have cards into packages of sellers, and puts the package
the owner picks into their Cardmarket cart for them to check out by hand.

- **Signing in is the owner's.** The owner signs in on Cardmarket's own page; the app never sees or stores
  the password (WebView2 may save it, as Edge does, in its encrypted profile). While signed in, it saves the
  session cookie to Credential Manager (service `Commander (cmdr) Cardmarket`) and adds it back at start. The
  Cardmarket window shares the app's WebView2 profile, as pywebview keeps one per process.
- **Handing the window over.** Cardmarket cannot be framed (`X-Frame-Options: SAMEORIGIN`, its challenge page
  too), so when a page the scan loads is signed out, or Cloudflare's check has not passed by itself after 8 s,
  the hidden scan window itself is shown, frameless, exactly over an empty pane of the Buy screen, and the
  progress line says what is needed. The scan waits, with no time limit, until a signed-in page loads (the
  session is then saved) or the owner presses Cancel; the window is hidden again and the page it wanted loads
  once more, in the same window and session. A form step the handover interrupted is redone from the start
  of the wants list sync, once. Cancel is also heard while a page loads (the load event is waited for a second
  at a time) and while a check is waited out, not only during a handover; a window that leaves five reads in
  a row unanswered during a handover is given up as a timeout, since each silent read leaves a thread behind.
  A failure inside the window (a script that throws, a .NET exception) is a browser error like any other, so
  a scan that meets one stops with what it has read. Probed live on 2026-11-09 (27 window checks, three runs):
  - The window is created `frameless`, `hidden`, without shadow, and owned by the app's window (WinForms
    `Owner`, set on the shared UI thread): it stays above the app only, hides when the app is minimised and
    comes back on restore.
  - pywebview's `move` and `resize` show a hidden window, so placing goes through `SetWindowPos` without
    activation, in physical pixels (pywebview makes the process DPI-aware): the app window's client origin
    plus the pane's CSS rectangle times `devicePixelRatio`. The page reports the rectangle when it shows the
    pane, on resize and on each status paint; the window follows the app's `moved` event.
  - Setting `ShowInTaskbar` makes WinForms recreate the window's handle; the taskbar button is removed by
    clearing `WS_EX_APPWINDOW` on the handle while hidden instead.
  - `show()` from the scan's thread activates the window, but its page had the keyboard focus only four runs
    in five, so the WebView2 control is given the focus after each show.
  - Only 100% display scaling on one monitor was tried.
- **The wants list.** The app creates or refills a list named for the purpose, imports the Don't Have text,
  then sets the owner's language and minimum condition through the bulk form, one quantity group at a time so
  quantities survive. It only ever changes that list.
- **The scan reads pages, one at a time, in a hidden window,** at least 4 seconds apart, never in parallel and
  never on a schedule. Pass 1 opens each wanted card's page with the owner's filters in the address and reads
  its cheapest offers (50, a setting). Pass 2 opens the wants-filtered stock page of the base sellers: those
  who carry the rarest cards (the fewest offers in pass 1), or the most of the wanted cards. A sign-in page or
  a challenge that does not pass by itself is handed to the owner (above); a page that does not read as
  expected stops the scan and says so. Neither is retried in a loop or worked around. The gap is counted from
  the end of the browser's last request, whichever step made it: the wants list sync and the scan share one
  clock on the browser, so the scan's first page waits out the gap after the sync's last request.
- **Shipping comes from Cardmarket's own tables,** fetched with `net.Fetcher`, shape-checked by `jsonshape`
  and cached for a week per origin country: the cheapest method from the seller's country whose weight and
  value limits fit the package, tracked over 25 €.
- **The solver** chooses offers to cover every wanted card at the least total of prices plus each chosen
  seller's shipping, exactly, and repeats for at most 1, 2, 3, … sellers, so the screen shows how the best
  total falls as sellers are added. Each pick shows its price against the card's Cardmarket trend price,
  which the card database already has.
- **The Buy screen.** A main view opened from the sidebar lists every deck's Don't Have cards with a tick per
  deck, takes the offer filters (language English, Japanese, both or any, minimum condition Near Mint or
  any, foil any, non-foil or foil; a blank Cardmarket field is a catch-all, so foil defaults to any) and
  scans in the background with a progress line and a Cancel button, which also ends a wait for the owner.
  A cancelled or stopped scan keeps what it read and solves it, marked partial. The curve shows one row per
  seller count; picking a row shows each seller's shipping method and each card's price against its trend
  price (the cheapest printing's EUR price in the card data). The choices, the base sellers (15) and the
  decks and cards left out per account are kept in `buy.json` beside the config file (format 3).
- **Language.** English is the default. Japanese is the second choice because the European market sells it
  cheaply; Both takes English and Japanese; Any (last, to save the most) asks Cardmarket nothing. The card page
  takes one bracketed field per language, `language[1]=1&language[7]=7`, as Cardmarket's own form sends it
  (probed 2026-11-09: 42 English and 8 Japanese offers for Both). The wants list's `idLanguages[]` select is
  `multiple`, so the sync selects each chosen option and the check after it accepts a want whose languages are
  exactly the chosen ones, in any order. The package already shows each card's language.
- **Seller location and delivery time.** Two more offer filters, both blank (any) by default. The owner ticks
  seller countries (Cardmarket's own ids, the shipping help API's too), which go in the card page address as
  `sellerCountry[<id>]` and also drop other countries' offers, since the wants-filtered stock pages are not
  filtered by country. The delivery time is at most 5 days, 1 week, 10 days or 2 weeks, judged on the
  seller's own estimate to the buyer: Cardmarket's form offers 2 to 6 days only, so `maxShippingTime` goes in
  the address for 5 days and every longer limit is applied after the offers are read, to the base sellers'
  choice and to the solver's offers alike. A seller that shows "-" for want of deliveries is judged by its
  country instead: the median of the other sellers' estimates from that country in the same scan (of an even
  count, the mean of the middle two rounded up), else Cardmarket's average for the route that every card-page
  row carries; only an offer with neither is left out, and the package says how many were. The package shows
  each seller's days (marked as an estimate when they come from its country) and when the slowest arrives.
- **Max price per card.** A fourth offer filter, blank (any) by default: the most one copy may cost, typed in
  euros (a dot or a comma, at most two decimals, 0,01 to 10 000 €). Cardmarket's form has no such limit, so it
  never goes in the address; `apply_limits` leaves out an offer priced above it (one priced at exactly the cap
  stays), for the base sellers' choice and the solver alike. A wanted card whose every otherwise allowed offer
  is dearer is not bought; the results list it under "Over your max price" with its cheapest price, apart from
  the cards with no offers at all. Pass 1 reads each card's cheapest offers, so that price is the card's
  lowest. Because the cap only judges offers already read, changing it re-judges and re-solves the last scan
  in the background instead of asking for a new one; the base sellers stay those chosen under the old cap, so a
  scan under the new cap can find a cheaper package. Any other filter still drops the results.
- **Cards ticked one by one.** Each deck's fold gives every Don't Have card its own tick, on by default, and
  the deck's tick is mixed when only some are on; ticking or unticking the deck sets all of its cards. A card
  in two decks is ticked per deck, and the quantity to buy is the sum of the ticked ones. The unticked cards
  are kept per account and deck beside the left-out decks, and a card that is no longer Don't Have is
  forgotten. A deck whose cards are all unticked is kept as a left-out deck.
- **The cart.** After the owner confirms a package (sellers, cards, prices, total), the app sends, for each
  offer, the request its own cart button sends, paced like the scan, then reads the cart back. It lists what
  was added, the offers that were gone (with Cardmarket's reason), cards the cart holds fewer copies of, and
  each package seller whose cart shipping differs from the estimate; a link opens the cart page in the owner's
  own browser for checkout. A gone offer is replaced by scanning again. The app adds only what the owner
  confirmed, skips an article already in the cart, and never removes anything, clears the cart or checks out.
  Probed live on 2026-11-09 with the owner's OK (one 0,04 € card added and removed again):
  - The button is a form whose submit Cardmarket's script turns into an ajax `POST` to
    `/en/Magic/AjaxAction/ShoppingCart_Add_AddArticlesFromMetacardPage` with `__cmtkn` (the session's token),
    `idArticle[<id>]` and `amount[<id>]`. The app sends that with `fetch` from a Cardmarket page; a form
    cloned into a page skips Cardmarket's handler and posts as a plain form. The cart page carries the token
    only in its items' forms, so the requests go out from the wants lists page, whose create-list form
    always has it.
  - The answer is HTTP 200 and an `<ajaxResponse>` XML of base64 fields: `resultType` success or error and a
    `systemMessage` alert. An article that does not exist answers error, "The item is not available.", and
    leaves the cart unchanged.
  - One request per offer, the owner's choice: each refusal gets its own answer, and nothing unprobed (several
    articles in one request) is sent.

The deciding reason is the owner's: their own method beats the Wizard, and only an optimiser that weighs
shipping against price can do that across 60 cards.

**Removal condition.** If Cardmarket blocks this use (a challenge that does not pass, a warning on the
account) or its terms come to forbid it, the scan and cart steps are removed and `cmdr toget --cardmarket`
stays as the way to the Wizard.

## Consequences

- The owner can buy a long wants list from fewer sellers at a lower total, and see why.
- The app depends on Cardmarket's undocumented page markup and help-site endpoint. Each reader is tested on
  trimmed real pages and stops the scan when a page does not match, so a redesign costs the scan, never the
  rest of the app.
- A live Cardmarket session is stored in Credential Manager: anyone with the owner's Windows account can use
  it until the server ends it. Signing out in the app removes it.
- The estimate can miss shipping rules that only the cart knows (a seller that must ship tracked), which the
  cart check points out after adding.
- Revisit if Cardmarket reopens its API to private users, publishes terms that cover this, or changes the
  pages so often that the readers cannot keep up.
