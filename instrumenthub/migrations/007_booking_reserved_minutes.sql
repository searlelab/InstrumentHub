ALTER TABLE bookings ADD COLUMN reserved_minutes INTEGER NOT NULL DEFAULT 0 CHECK(reserved_minutes >= 0);

UPDATE bookings
SET reserved_minutes = COALESCE((
  SELECT SUM(reserved_slots.nominal_minutes)
  FROM reserved_slots
  WHERE reserved_slots.calendar_entry_id = bookings.calendar_entry_id
), 0);
