ALTER TABLE bookings ADD COLUMN column_setup TEXT NOT NULL DEFAULT '';
ALTER TABLE bookings ADD COLUMN libraries_present INTEGER NOT NULL DEFAULT 0 CHECK(libraries_present IN (0,1));
ALTER TABLE bookings ADD COLUMN gradient_minutes INTEGER;

CREATE TABLE notifications (
  id INTEGER PRIMARY KEY,
  recipient_user_id INTEGER NOT NULL REFERENCES users(id),
  event_key TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  link TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  read_at TEXT,
  UNIQUE(recipient_user_id, event_key)
);
CREATE INDEX notifications_recipient_created ON notifications(recipient_user_id, created_at DESC);

CREATE TABLE notification_deliveries (
  id INTEGER PRIMARY KEY,
  notification_id INTEGER NOT NULL UNIQUE REFERENCES notifications(id) ON DELETE CASCADE,
  state TEXT NOT NULL CHECK(state IN ('pending','sent','failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
  next_attempt_at TEXT NOT NULL,
  last_error TEXT NOT NULL DEFAULT '',
  sent_at TEXT
);
CREATE INDEX notification_deliveries_pending ON notification_deliveries(state, next_attempt_at);

CREATE TABLE maintenance_templates (
  id INTEGER PRIMARY KEY,
  instrument_id INTEGER NOT NULL REFERENCES instruments(id),
  title TEXT NOT NULL,
  interval_days INTEGER NOT NULL CHECK(interval_days BETWEEN 1 AND 3650),
  expected_minutes INTEGER NOT NULL CHECK(expected_minutes > 0),
  assignee_user_id INTEGER REFERENCES users(id),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  next_due_date TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE maintenance_tasks (
  id INTEGER PRIMARY KEY,
  template_id INTEGER NOT NULL REFERENCES maintenance_templates(id),
  due_date TEXT NOT NULL,
  assignee_user_id INTEGER REFERENCES users(id),
  state TEXT NOT NULL CHECK(state IN ('open','completed','skipped')),
  completed_at TEXT,
  completion_reason TEXT NOT NULL DEFAULT '',
  calendar_entry_id INTEGER UNIQUE REFERENCES calendar_entries(id),
  created_at TEXT NOT NULL,
  UNIQUE(template_id, due_date)
);
CREATE INDEX maintenance_tasks_due ON maintenance_tasks(state, due_date, id);
