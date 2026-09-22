"""Exception types shared across inventory_lib."""


class InventoryError(Exception):
    """Base class for all inventory-related errors."""


class InsufficientStockError(InventoryError):
    """Raised when a reservation would exceed available stock for a SKU."""


class ReservationNotFoundError(InventoryError):
    """Raised when a reservation id does not exist."""


class InvalidQuantityError(InventoryError):
    """Raised when a requested quantity is zero, negative, or too large."""


class ImportValidationError(Exception):
    """Raised by the CSV importer when a row cannot be parsed at all.

    Individual bad rows are normally reported as `ImportError` entries
    instead of raising; this is reserved for structural problems such as
    a missing header row.
    """
