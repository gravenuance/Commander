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
- **Cart.** Clicking an offer's cart button added it; the cart page (`/en/Magic/ShoppingCart`) shows each
  seller's items, article value, shipping method and cost, total and estimated arrival. "Remove articles from
  this seller" (`ShoppingCart_RemoveSeller`) and per-article remove (`ShoppingCart_RemoveArticle`) took it out
  again. The cart already held an item the owner had put there, which stayed untouched.
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

- **Signing in is the owner's.** The app opens Cardmarket in its own window and the owner signs in there. The
  app never sees or stores the password (WebView2 may save it, as Edge does, in its encrypted profile). While
  signed in, it saves the session cookie to Credential Manager (service `Commander (cmdr) Cardmarket`) and
  adds it back at start. The Cardmarket windows share the app's WebView2 profile, as pywebview keeps one per
  process.
- **The wants list.** The app creates or refills a list named for the purpose, imports the Don't Have text,
  then sets the owner's language and minimum condition through the bulk form, one quantity group at a time so
  quantities survive. It only ever changes that list.
- **The scan reads pages, one at a time, in a hidden window,** at least 4 seconds apart, never in parallel and
  never on a schedule. Pass 1 opens each wanted card's page with the owner's filters in the address and reads
  its cheapest offers (50, a setting). Pass 2 opens the wants-filtered stock page of the base sellers: those
  who carry the rarest cards (the fewest offers in pass 1), or the most of the wanted cards. A challenge, a
  sign-in page or a page that does not read as expected stops the scan and says so; it is never retried in a
  loop or worked around.
- **Shipping comes from Cardmarket's own tables,** fetched with `net.Fetcher`, shape-checked by `jsonshape`
  and cached for a week per origin country: the cheapest method from the seller's country whose weight and
  value limits fit the package, tracked over 25 €.
- **The solver** chooses offers to cover every wanted card at the least total of prices plus each chosen
  seller's shipping, exactly, and repeats for at most 1, 2, 3, … sellers, so the screen shows how the best
  total falls as sellers are added. Each pick shows its price against the card's Cardmarket trend price,
  which the card database already has.
- **The cart.** After the owner confirms a package (sellers, cards, prices, total), the app clicks each
  offer's own cart button, paced like the scan, then reads the cart: an offer that is gone is listed and can
  be solved again among the sellers already in the cart, and a seller whose shipping differs from the estimate
  is pointed out. It then shows the cart page for checkout. The app adds only what the owner confirmed and
  removes only what it added; it never clears the cart or checks out.

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
