CREATE TABLE reservation_overlap_migration_guard (
  must_be_zero INTEGER NOT NULL CHECK(must_be_zero = 0)
);
INSERT INTO reservation_overlap_migration_guard(must_be_zero)
SELECT 1
WHERE EXISTS (
  SELECT 1
  FROM reserved_slots AS first
  JOIN reserved_slots AS second
    ON first.instrument_id = second.instrument_id
   AND first.id < second.id
   AND first.slot_start < second.slot_end
   AND second.slot_start < first.slot_end
);
DROP TABLE reservation_overlap_migration_guard;

CREATE TRIGGER reserved_slots_no_range_overlap_insert
BEFORE INSERT ON reserved_slots
WHEN EXISTS (
  SELECT 1 FROM reserved_slots AS existing
  WHERE existing.instrument_id = NEW.instrument_id
    AND existing.slot_start < NEW.slot_end
    AND NEW.slot_start < existing.slot_end
)
BEGIN
  SELECT RAISE(ABORT, 'instrument reservation range overlap');
END;

CREATE TRIGGER reserved_slots_no_range_overlap_update
BEFORE UPDATE OF instrument_id, slot_start, slot_end ON reserved_slots
WHEN EXISTS (
  SELECT 1 FROM reserved_slots AS existing
  WHERE existing.id != OLD.id
    AND existing.instrument_id = NEW.instrument_id
    AND existing.slot_start < NEW.slot_end
    AND NEW.slot_start < existing.slot_end
)
BEGIN
  SELECT RAISE(ABORT, 'instrument reservation range overlap');
END;
