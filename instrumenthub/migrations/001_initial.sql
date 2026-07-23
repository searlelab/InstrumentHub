CREATE TABLE users (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL, initials TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '', accent_color TEXT NOT NULL DEFAULT '#2563eb',
  role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE sessions (
  id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE login_attempts (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL, remote_addr TEXT NOT NULL,
  attempted_at TEXT NOT NULL, succeeded INTEGER NOT NULL CHECK(succeeded IN (0,1))
);
CREATE INDEX login_attempts_lookup ON login_attempts(username, remote_addr, attempted_at);
CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE pis (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE instruments (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  training_required INTEGER NOT NULL DEFAULT 0 CHECK(training_required IN (0,1)),
  unavailable INTEGER NOT NULL DEFAULT 0 CHECK(unavailable IN (0,1))
);
CREATE TABLE block_definitions (
  id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL REFERENCES instruments(id),
  name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  effective_from TEXT NOT NULL, effective_to TEXT, sort_order INTEGER NOT NULL,
  UNIQUE(instrument_id, effective_from, sort_order)
);
CREATE TABLE projects (
  id INTEGER PRIMARY KEY, pi_id INTEGER NOT NULL REFERENCES pis(id),
  code TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
  max_consecutive_minutes INTEGER CHECK(max_consecutive_minutes IS NULL OR max_consecutive_minutes > 0),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE grants (
  id INTEGER PRIMARY KEY, pi_id INTEGER NOT NULL REFERENCES pis(id),
  number TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE project_grants (
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  grant_id INTEGER NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
  PRIMARY KEY(project_id, grant_id)
);
CREATE TABLE instrument_training (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
  trained_at TEXT NOT NULL, revoked_at TEXT,
  PRIMARY KEY(user_id, instrument_id)
);
CREATE TABLE allotments (
  id INTEGER PRIMARY KEY, scope_type TEXT NOT NULL CHECK(scope_type IN ('pi','project')),
  scope_id INTEGER NOT NULL, instrument_id INTEGER REFERENCES instruments(id),
  period_start TEXT NOT NULL, period_end TEXT NOT NULL, minutes_allowed INTEGER NOT NULL CHECK(minutes_allowed > 0),
  CHECK(period_end >= period_start)
);
CREATE INDEX allotments_lookup ON allotments(scope_type, scope_id, instrument_id, period_start, period_end);
CREATE TABLE calendar_entries (
  id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL REFERENCES instruments(id),
  entry_type TEXT NOT NULL CHECK(entry_type IN ('hold','booking','maintenance','outage')),
  state TEXT NOT NULL, title TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE reserved_slots (
  id INTEGER PRIMARY KEY, calendar_entry_id INTEGER NOT NULL REFERENCES calendar_entries(id) ON DELETE CASCADE,
  instrument_id INTEGER NOT NULL REFERENCES instruments(id),
  slot_start TEXT NOT NULL, slot_end TEXT NOT NULL, nominal_minutes INTEGER NOT NULL CHECK(nominal_minutes > 0),
  requested_slot_start TEXT NOT NULL,
  UNIQUE(instrument_id, slot_start), UNIQUE(calendar_entry_id, slot_start), CHECK(slot_end > slot_start)
);
CREATE INDEX reserved_slots_range ON reserved_slots(instrument_id, slot_start, slot_end);
CREATE TABLE bookings (
  id INTEGER PRIMARY KEY, calendar_entry_id INTEGER NOT NULL UNIQUE REFERENCES calendar_entries(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id), pi_id INTEGER NOT NULL REFERENCES pis(id),
  project_id INTEGER NOT NULL REFERENCES projects(id), grant_id INTEGER REFERENCES grants(id),
  experiment_code TEXT NOT NULL, expected_minutes INTEGER NOT NULL CHECK(expected_minutes > 0),
  sample_category TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', admin_notes TEXT NOT NULL DEFAULT '',
  override_required INTEGER NOT NULL DEFAULT 0 CHECK(override_required IN (0,1)),
  requested_start TEXT NOT NULL, requested_end TEXT NOT NULL,
  queue_order INTEGER, pre_pause_state TEXT
);
CREATE TABLE holds (
  id INTEGER PRIMARY KEY, calendar_entry_id INTEGER NOT NULL UNIQUE REFERENCES calendar_entries(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id), token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL
);
CREATE TABLE maintenance (
  id INTEGER PRIMARY KEY, calendar_entry_id INTEGER NOT NULL UNIQUE REFERENCES calendar_entries(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('routine','emergency','outage')),
  known_duration INTEGER NOT NULL DEFAULT 1 CHECK(known_duration IN (0,1)), reason TEXT NOT NULL, restart_at TEXT
);
CREATE TABLE approval_decisions (
  id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id),
  administrator_id INTEGER NOT NULL REFERENCES users(id),
  decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','expired')),
  reason TEXT NOT NULL, decided_at TEXT NOT NULL
);
CREATE TABLE schedule_change_drafts (
  id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL REFERENCES instruments(id),
  kind TEXT NOT NULL CHECK(kind IN ('known_outage','unknown_outage','resume')),
  status TEXT NOT NULL CHECK(status IN ('draft','applied','discarded')),
  reason TEXT NOT NULL, payload_json TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, applied_at TEXT
);
CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY, actor_user_id INTEGER REFERENCES users(id), action TEXT NOT NULL,
  entity_type TEXT NOT NULL, entity_id TEXT, reason TEXT NOT NULL DEFAULT '',
  before_json TEXT NOT NULL DEFAULT '{}', after_json TEXT NOT NULL DEFAULT '{}',
  request_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX audit_events_entity ON audit_events(entity_type, entity_id, created_at);

