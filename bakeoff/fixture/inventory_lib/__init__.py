"""inventory_lib: a small inventory + order-pricing library.

Modules:
    models     -- Item, OrderLine, Order data structures.
    discounts  -- tiered volume-discount schedule.
    pricing    -- combines subtotal, discount and tax into a PriceBreakdown.
    inventory  -- stock levels and reservation lifecycle.
    importer   -- CSV catalog importer (builds Item + Inventory from a file).
    cli        -- a tiny command-line front end tying the pieces together.
"""

__version__ = "0.1.0"
