package outgoing

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/meowcaller-poc/internal/config"
	"github.com/purpshell/meowcaller"
)

// writeTempSessions writes a fake OpenClaw sessions.json and returns its path.
func writeTempSessions(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "sessions.json")
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatalf("write sessions.json: %v", err)
	}
	return p
}

const sampleSessions = `{
  "agent:main:main": {"sessionId": "x"},
  "agent:main:whatsapp:direct:+6281234567890": {"sessionId": "a"},
  "agent:main:direct:+6281234567801": {"sessionId": "b"},
  "agent:main:direct:+6281234567802": {
    "sessionId": "c",
    "origin": {"from": "+6281234567802", "to": "+6281234567803"}
  },
  "agent:main:direct:+999999999999999": {"sessionId": "d"},
  "agent:main:direct:+999999999999998": {"sessionId": "e"},
  "agent:main:direct:+999999999999997": {"sessionId": "f"},
  "agent:main:direct:99887766": {"sessionId": "g"},
  "agent:main:direct:+081234567804": {"sessionId": "h"},
  "agent:main:direct:+14155551234": {"sessionId": "i"},
  "agent:main:telegram:direct:99887766": {"sessionId": "j"}
}`

func TestExtractPhonesFromSessions(t *testing.T) {
	p := writeTempSessions(t, sampleSessions)
	got := extractPhonesFromSessions(p)

	want := map[string]bool{
		"6281234567890": true,
		"6281234567801":  true,
		"6281234567802":   true,
		"6281234567803": true, // agent's own number from origin.to
	}
	if len(got) != len(want) {
		t.Fatalf("got %d numbers %v, want %d", len(got), got, len(want))
	}
	for _, n := range got {
		if !want[n] {
			t.Errorf("unexpected number extracted: %s", n)
		}
	}
	// WhatsApp LIDs, telegram IDs, leading-zero and non-62 numbers must never appear.
	for _, bad := range []string{
		"999999999999999", "999999999999998", "999999999999997",
		"99887766", "081234567804", "14155551234",
	} {
		for _, n := range got {
			if n == bad {
				t.Errorf("LID/non-dialable number leaked into allowlist: %s", bad)
			}
		}
	}
}

func TestExtractPhonesMissingFile(t *testing.T) {
	if got := extractPhonesFromSessions(filepath.Join(t.TempDir(), "nope.json")); got != nil {
		t.Fatalf("expected nil for missing file, got %v", got)
	}
}

func TestExtractPhonesInvalidJSON(t *testing.T) {
	p := writeTempSessions(t, `{not json`)
	if got := extractPhonesFromSessions(p); got != nil {
		t.Fatalf("expected nil for invalid json, got %v", got)
	}
}

func TestAllowlistFromSessionStore(t *testing.T) {
	p := writeTempSessions(t, sampleSessions)
	m := &Manager{
		cfg: config.OutgoingConfig{
			Allowlist:           true, // allowlist on: only session-store numbers are callable
			SessionStorePath:    p,
			SessionAllowlistTTL: time.Minute,
		},
		client: &meowcaller.Client{},
	}
	got := m.Allowlist()
	if len(got) != 4 {
		t.Fatalf("session allowlist len = %d, want 4: %v", len(got), got)
	}
	for _, want := range []string{"6281234567890", "6281234567801", "6281234567802", "6281234567803"} {
		found := false
		for _, n := range got {
			if n == want {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("session allowlist missing %s: %v", want, got)
		}
	}
	if !m.isAllowlisted("6281234567801") {
		t.Error("session-derived number must be callable")
	}
	if m.isAllowlisted("14155551234") {
		t.Error("non-62 number must not be callable via session allowlist")
	}
}

func TestAllowlistOffAcceptsAnyNumber(t *testing.T) {
	p := writeTempSessions(t, sampleSessions)
	m := &Manager{
		cfg: config.OutgoingConfig{
			Allowlist:        false, // allowlist off: session store is ignored
			SessionStorePath: p,
		},
		client: &meowcaller.Client{},
	}
	if got := m.Allowlist(); got != nil {
		t.Fatalf("allowlist off should return nil, got %v", got)
	}
	if err := m.Validate("6281111111111"); err != nil {
		t.Errorf("any valid number must be callable when allowlist is off: %v", err)
	}
}

func TestSessionAllowlistCacheTTL(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "sessions.json")
	os.WriteFile(p, []byte(`{"agent:main:direct:+6281111111111":{}}`), 0o644)

	m := &Manager{
		cfg: config.OutgoingConfig{
			SessionStorePath:    p,
			SessionAllowlistTTL: 30 * time.Second,
		},
		client: &meowcaller.Client{},
	}
	if got := m.sessionAllowlist(); len(got) != 1 {
		t.Fatalf("initial load: got %v", got)
	}
	// Within TTL: file changes must not be picked up.
	os.WriteFile(p, []byte(`{"agent:main:direct:+6282222222222":{}}`), 0o644)
	if got := m.sessionAllowlist(); len(got) != 1 || got[0] != "6281111111111" {
		t.Fatalf("cache not honored: got %v", got)
	}
	// After TTL: reload picks up the change.
	m.sessionCachedAt = time.Now().Add(-time.Minute)
	if got := m.sessionAllowlist(); len(got) != 1 || got[0] != "6282222222222" {
		t.Fatalf("ttl expiry did not reload: got %v", got)
	}
}

func TestSessionAllowlistDisabledWhenPathEmpty(t *testing.T) {
	m := &Manager{cfg: config.OutgoingConfig{Allowlist: true}}
	if got := m.sessionAllowlist(); got != nil {
		t.Fatalf("expected nil when no session store path, got %v", got)
	}
	if got := m.Allowlist(); got != nil {
		t.Fatalf("allowlist on with no session store must yield no callable numbers, got %v", got)
	}
}
