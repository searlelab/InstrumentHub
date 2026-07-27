CREATE TABLE instrument_classes (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE class_block_definitions (
  id INTEGER PRIMARY KEY,
  instrument_class_id INTEGER NOT NULL REFERENCES instrument_classes(id),
  name TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  sort_order INTEGER NOT NULL,
  UNIQUE(instrument_class_id, effective_from, sort_order)
);

INSERT INTO instrument_classes(id, name) VALUES (1, 'Mass Spectrometer');

ALTER TABLE instruments ADD COLUMN instrument_class_id INTEGER REFERENCES instrument_classes(id);

UPDATE instruments
SET instrument_class_id = 1
WHERE instrument_class_id IS NULL;

INSERT INTO class_block_definitions(
  instrument_class_id, name, start_time, end_time, effective_from, effective_to, sort_order
)
SELECT 1, block.name, block.start_time, block.end_time, block.effective_from, block.effective_to, block.sort_order
FROM block_definitions AS block
WHERE block.instrument_id = (SELECT MIN(id) FROM instruments);

INSERT INTO instrument_classes(id, name)
SELECT 1000000 + instrument.id, 'Mass Spectrometer — legacy schedule for ' || instrument.name
FROM instruments AS instrument
WHERE instrument.id != (SELECT MIN(id) FROM instruments)
  AND (
    EXISTS (
      SELECT 1
      FROM block_definitions AS candidate
      WHERE candidate.instrument_id = instrument.id
        AND NOT EXISTS (
          SELECT 1
          FROM block_definitions AS baseline
          WHERE baseline.instrument_id = (SELECT MIN(id) FROM instruments)
            AND baseline.name = candidate.name
            AND baseline.start_time = candidate.start_time
            AND baseline.end_time = candidate.end_time
            AND baseline.effective_from = candidate.effective_from
            AND COALESCE(baseline.effective_to, '') = COALESCE(candidate.effective_to, '')
            AND baseline.sort_order = candidate.sort_order
        )
    )
    OR EXISTS (
      SELECT 1
      FROM block_definitions AS baseline
      WHERE baseline.instrument_id = (SELECT MIN(id) FROM instruments)
        AND NOT EXISTS (
          SELECT 1
          FROM block_definitions AS candidate
          WHERE candidate.instrument_id = instrument.id
            AND candidate.name = baseline.name
            AND candidate.start_time = baseline.start_time
            AND candidate.end_time = baseline.end_time
            AND candidate.effective_from = baseline.effective_from
            AND COALESCE(candidate.effective_to, '') = COALESCE(baseline.effective_to, '')
            AND candidate.sort_order = baseline.sort_order
        )
    )
  );

UPDATE instruments
SET instrument_class_id = 1000000 + id
WHERE id IN (
  SELECT instrument.id
  FROM instruments AS instrument
  JOIN instrument_classes AS instrument_class ON instrument_class.id = 1000000 + instrument.id
);

INSERT INTO class_block_definitions(
  instrument_class_id, name, start_time, end_time, effective_from, effective_to, sort_order
)
SELECT 1000000 + block.instrument_id, block.name, block.start_time, block.end_time, block.effective_from, block.effective_to, block.sort_order
FROM block_definitions AS block
JOIN instruments AS instrument ON instrument.id = block.instrument_id
WHERE instrument.instrument_class_id = 1000000 + instrument.id;
