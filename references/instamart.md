# Instamart orders

Source `instamart`, tab `Instamart`. Emails from `noreply@instamart.in` whose subject contains "delivered" ("Your Instamart order was successfully delivered"). The merchant is always `Instamart`.

## What the script reads from the bill

| Bill line | Example | Becomes |
|---|---|---|
| Item: `<qty> x <name>` with the line total (the quantity comes first) | `6 x Coca-Cola Zero Sugar Diet Drink  ₹240.00` | ONE row: name `Coca-Cola Zero Sugar Diet Drink`, quantity 6, item_price 240. Never 6 rows. |
| `Item Bill` | `₹240.00` | the items subtotal; used only as a check, never a row |
| Charges | `Handling Fee`, `Convenience Fee`, `Delivery Partner Fee`, `Small Cart Fee` | not a row; spread across items |
| `Grand Total` | `₹269.00` | `bill_total`; the item rows of an order add up to this |

## Naming grocery items

**Use the minimal standard name: what the product is, in one or two words from `raw`.** Drop the brand, variant, size and pack count. Keep the brand only when it *is* the everyday name (`Coca-Cola`, `Maggi`).

| raw | name |
|---|---|
| `Coca-Cola Zero Sugar Diet Drink` | `Coca-Cola` |
| `Gold Flake Kings Blue Cigarette` | `Cigarette` |
| `Amul Taaza Toned Milk 500 ml` | `Milk` |
| `Lay's India's Magic Masala Potato Chips (Pack of 2)` | `Chips` |
| `Fresh Onion (Pyaz) 1 kg` | `Onion` |
| `nectr Indian Tomato (Nati)` | `Tomato` |
| `Milky Mist Greek Yogurt Cup` | `Greek Yogurt` |
| `SUPREME HARVEST Rajma Kashmiri (Capparadavare)` | `Rajma` |
| `Dettol Original Liquid Handwash Refill 750 ml` | `Handwash` |

Only remove words; never add or translate them. For example `Pyaz` can be dropped, but `Onion` must not become `Onions`.

**Reuse names.** If `similar` lists the same product (another brand, variant or pack size of the same thing), use that name exactly.

## Categories for grocery items

Fresh produce is **always** `fruits_vegetables`, never `groceries`, even when it has a brand like `nectr`. `groceries` is for packaged staples.

| category | use for |
|---|---|
| `beverages` | soft drinks, juices, water, energy drinks, tea, coffee |
| `snacks` | chips, biscuits, namkeen, chocolates, instant noodles |
| `desserts` | ice cream, frozen desserts, sweets |
| `groceries` | staples: atta, rice, dal, oil, spices, sugar, salt, sauces, ready-to-cook |
| `fruits_vegetables` | fresh fruits, vegetables, herbs, whatever the brand (`nectr Avocado`, `nectr Indian Tomato (Nati)`, `Ooty Carrot`) |
| `dairy_bakery` | milk, curd, paneer, butter, cheese, bread, bakery |
| `meat_fish_eggs` | chicken, mutton, fish, eggs |
| `personal_care` | soap, shampoo, toothpaste, skincare, sanitary products |
| `household` | detergents, cleaners, tissues, foil, batteries, utensils |
| `health` | medicines, supplements, first aid |
| `tobacco` | cigarettes, rolling paper, lighters |
| `alcohol` | beer, wine, spirits |
| `other` | anything that clearly fits none of the above |
