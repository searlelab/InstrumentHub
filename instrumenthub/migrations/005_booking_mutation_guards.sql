ALTER TABLE bookings ADD COLUMN administratively_altered INTEGER NOT NULL DEFAULT 0 CHECK(administratively_altered IN (0,1));
ALTER TABLE bookings ADD COLUMN approval_round INTEGER NOT NULL DEFAULT 1 CHECK(approval_round > 0);
ALTER TABLE approval_decisions ADD COLUMN approval_round INTEGER NOT NULL DEFAULT 1 CHECK(approval_round > 0);
UPDATE approval_decisions AS current
SET approval_round = (
  SELECT COUNT(*)
  FROM approval_decisions AS earlier
  WHERE earlier.booking_id = current.booking_id
    AND (earlier.decided_at < current.decided_at OR (earlier.decided_at = current.decided_at AND earlier.id <= current.id))
);
UPDATE bookings
SET approval_round = CASE
  WHEN NOT EXISTS (SELECT 1 FROM approval_decisions WHERE approval_decisions.booking_id = bookings.id) THEN 1
  WHEN (SELECT state FROM calendar_entries WHERE calendar_entries.id = bookings.calendar_entry_id) = 'tentative'
    THEN (SELECT MAX(approval_round) + 1 FROM approval_decisions WHERE approval_decisions.booking_id = bookings.id)
  ELSE (SELECT MAX(approval_round) FROM approval_decisions WHERE approval_decisions.booking_id = bookings.id)
END;
CREATE UNIQUE INDEX approval_decisions_one_per_round ON approval_decisions(booking_id, approval_round);
