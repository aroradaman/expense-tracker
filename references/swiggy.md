# Swiggy food orders

Source `swiggy`, tab `Swiggy`. Emails from `noreply@swiggy.in` whose subject contains "delivered" (for example "Your Swiggy order was delivered superfast"). Each email is one restaurant order.

## What the script reads from the bill

| Bill line | Example | Becomes |
|---|---|---|
| Item: `<name> x<qty>` with the line total | `Garlic Naan x2  ₹298` | one row: name `Garlic Naan`, quantity 2, item_price 298 |
| Free item | `Brownie Cake x1  FREE` | one row with price 0; it gets no share of the charges |
| Charges | `Restaurant Packaging`, `Platform fee with GST`, `Express Fee`, `Delivery Fee`, `Taxes` | not a row; spread across items |
| Discounts | `Discount Applied (FLAT100)  - ₹99.98`, `Extra 13% off (with Swiggy One)` | not a row; they reduce item prices |
| Total | `Paid Via Credit/Debit card  ₹660` | `bill_total`; the item rows of an order add up to this |

The merchant is the restaurant name (for example `1947 Restaurant`).

Swiggy has used two email layouts, and the script reads both:
- **2026 and later:** the "BILL DETAILS" card, as in the table above. Gourmet orders can list add-ons under an item (`With Cheese Slice (₹30)`); they are already part of that item's price and never become rows. Some emails also contain `Item Total` (a subtotal) and `Line Separator` rows; neither is a charge.
- **2024–2025:** an "Item Name | Quantity | Price" table, followed by `Item Total:`, the charges (`Order Packing Charges:`, `Delivery partner fee:`, `Discount Applied (…):`, `Taxes:` …), `Paid Via …:` and `Order Total:`. The quantity has its own column, so item names never contain it.

## Naming restaurant items

**Use the minimal standard name: the kind of dish, usually one or two words taken from `raw`.** Drop flavours, fillings, proteins, restaurant styles, sizes and marketing words. The sheet groups spend by these names, so `Sandwich` is far more useful than ten sandwich variants.

| raw | name |
|---|---|
| `Crunchy Butterscotch Milkshake [350ml]` | `Milkshake` |
| `Bombay Masala Cheese Grill Sandwich ( Serves 1 )` | `Sandwich` |
| `Truffle Shroom Burger` | `Burger` |
| `Overloaded Veggies Flatzz Pizza` | `Pizza` |
| `Classic with Butter - Made with Maggi®` | `Maggi` |
| `Veg Pahari Fresh Pan Fried Momo in Tomato Garlic Sauce` | `Momos` (reuse) |
| `Butter Masala Dosa` | `Dosa` |
| `Classic Fried Rice - Veg (Serves 1-2)` | `Rice` |
| `Bowl Rnr Special Donne Chicken Boneless Biryani` | `Biryani` |
| `Special Butter Garlic Naan` | `Bread` (reuse) |
| `Nutty Butterscotch Boba Sundae` | `Ice Cream` (reuse) |
| `White Chocolate Blondie Ice Cream` | `Ice Cream` |
| `Panner Dhudiya Kebab` | `Kebab` |

**Always check `similar` first.** If it holds the standard name for this kind of dish, use it exactly, even if the spelling in `raw` differs: `Momo` → `Momos`, `Dal Makhni` → `Dal Makhani`, `Thickshake` → `Milkshake`, `Dimsum` → `Momos`.

Standard names already in use (reuse these exactly): `Milkshake`, `Chai`, `Lassi`, `Coca-Cola`, `Hot Chocolate`, `Mojito`, `Smoothie`, `Ice Cream`, `Rasmalai`, `Ghewar`, `Gulab Jamun`, `Brownie`, `Cheesecake`, `Cupcake`, `Cookie`, `Pancake`, `Sandwich`, `Burger`, `Pizza`, `Pasta`, `Maggi`, `Noodles`, `Rice`, `Biryani`, `Dosa`, `Bread`, `Kulcha`, `Wrap`, `Roll`, `Soup`, `Dal Makhani`, `Paneer Curry`, `Soya Chaap`, `Khichdi`, `Momos`, `Samosa`, `Kachori`, `Kebab`, `Tikka`, `Chilli Paneer`, `Manchurian`, `Vada Pav`, `Chutney`, `Raita`.

**Keep the dish type, never swap it for another.** A shared word or ingredient is not the same dish:

| raw | wrong | right |
|---|---|---|
| `Spicy Cheese Vada Pav` | `Burger` | `Vada Pav` |
| `Cheesy Butter Garlic` (at a pizza place) | `Maggi` | `Bread` (reuse) |
| `Crunchy Butterscotch Milkshake` | `Ice Cream` | `Milkshake`: a drink, not a scoop |
| `Butterscotch Blast Gudbud Jar` | `Milkshake` | `Ice Cream`: a jar dessert, not a drink |

Keep two words only when one word would lose the dish (`Vada Pav`, `Chole Bhature`, `Gulab Jamun`, `Dal Makhani`).

**Groups that share one name:** every ice-cream dessert (sundae, gudbud, McFlurry, gelato, scoop) is `Ice Cream`; naan, roti and garlic bread are `Bread`.

## Categories for restaurant items

| category | use for |
|---|---|
| `meals` | main dishes, breads (naan, roti, garlic bread), rice, curries, biryani, pizza, pasta, noodles, Maggi, burgers, wraps, rolls, sandwiches, thalis, soups |
| `snacks` | starters: kebab, tikka, 65, chilli, manchurian, chaat, momos, samosa, kachori, fries; also chutneys and sides |
| `desserts` | ice cream, sundaes, cakes, brownies, sweets, kulfi |
| `beverages` | anything you drink: milkshakes and shakes (even `Ice Cream Shake` or `Blondie Milk Shake`), coffee, tea, juices, soft drinks, lassi |
| `other` | anything that clearly fits none of the above |

Starters are always `snacks`, even paneer or chicken ones (`Paneer Kebab`, `Chicken Tikka`, `Paneer 65`, `Dragon Paneer`). If a word like `shake` or `lassi` is in the name, it's `beverages`, not `desserts`. Otherwise, if you're unsure between `meals` and `snacks`, choose `meals`.
