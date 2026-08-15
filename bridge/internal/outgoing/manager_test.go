package outgoing

import (
	"os"
	"strings"
	"testing"
	"time"

	"github.com/meowcaller-poc/internal/config"
	"github.com/meowcaller-poc/internal/eventspool"
)

func testManager(t *testing.T, cfg config.OutgoingConfig) *Manager {
	t.Helper()
	spool, _ := eventspool.NewSpool(t.TempDir())
	return NewManager(cfg, nil, spool)
}

// testManagerWithSessions builds a manager whose session store is a real
// (fake) sessions.json file, for allowlist-mode tests.
func testManagerWithSessions(t *testing.T, cfg config.OutgoingConfig, sessionsContent string) *Manager {
	t.Helper()
	dir := t.TempDir()
	store := dir + "/sessions.json"
	if err := os.WriteFile(store, []byte(sessionsContent), 0o644); err != nil {
		t.Fatalf("write sessions.json: %v", err)
	}
	cfg.SessionStorePath = store
	return testManager(t, cfg)
}

// The client is nil in these tests — Validate must never touch it.

func TestValidatePhoneFormat(t *testing.T) {
	m := testManager(t, config.OutgoingConfig{
		Allowlist:       false, // format tests must not depend on the allowlist
		MaxCallsPerHour: 10,
	})

	valid := []string{
		"6281234567890",
		"14155552671", // US
		"81312345678", // local without country code is technically allowed by regex
	}
	for _, phone := range valid {
		if err := m.Validate(phone); err != nil {
			t.Errorf("Validate(%q) unexpected error: %v", phone, err)
		}
	}

	invalid := []string{
		"",             // empty
		"+6281234567890", // leading +
		"62812 3456 7890", // spaces
		"62812-34567890",  // dash
		"0123456789",      // leading zero
		"123",             // too short
		"12345678901234567890", // too long
		"abc1234567",      // letters
		"62812.34567890",  // dot
	}
	for _, phone := range invalid {
		if err := m.Validate(phone); err == nil {
			t.Errorf("Validate(%q) expected error, got nil", phone)
		}
	}
}

func TestValidateAllowlist(t *testing.T) {
	m := testManagerWithSessions(t, config.OutgoingConfig{
		Allowlist:       true, // only numbers with a direct chat session may be called
		MaxCallsPerHour: 10,
	}, `{"agent:main:direct:+6281234567890": {"sessionId": "a"}}`)

	if err := m.Validate("6281234567890"); err != nil {
		t.Errorf("allowlisted number rejected: %v", err)
	}
	err := m.Validate("6281111111111")
	if err == nil {
		t.Fatal("non-allowlisted number accepted")
	}
	if !strings.Contains(err.Error(), "allowlist") {
		t.Errorf("error should mention allowlist, got: %v", err)
	}
}

func TestValidateEmptyAllowlistRejectsEverything(t *testing.T) {
	m := testManagerWithSessions(t, config.OutgoingConfig{
		Allowlist:       true, // allowlist on, but the session store has no numbers
		MaxCallsPerHour: 10,
	}, `{}`)
	if err := m.Validate("6281234567890"); err == nil {
		t.Error("empty session store should reject all numbers in allowlist mode")
	}
}

func TestRateLimit(t *testing.T) {
	m := testManager(t, config.OutgoingConfig{
		Allowlist:       false,
		MaxCallsPerHour: 3,
	})

	// First 3 calls pass.
	for i := 0; i < 3; i++ {
		if err := m.Validate("6281234567890"); err != nil {
			t.Fatalf("call %d rejected: %v", i+1, err)
		}
		m.recordCall(time.Now())
	}
	// 4th call within the hour is rejected.
	err := m.Validate("6281234567890")
	if err == nil {
		t.Fatal("4th call in the hour accepted, want rate limit rejection")
	}
	if !strings.Contains(err.Error(), "rate limit") {
		t.Errorf("error should mention rate limit, got: %v", err)
	}
}

func TestRateLimitWindowExpiry(t *testing.T) {
	m := testManager(t, config.OutgoingConfig{
		Allowlist:       false,
		MaxCallsPerHour: 1,
	})

	if err := m.Validate("6281234567890"); err != nil {
		t.Fatalf("first call rejected: %v", err)
	}
	m.recordCall(time.Now().Add(-2 * time.Hour)) // old call, outside window

	// Old timestamp pruned, so the next call is allowed again.
	if err := m.Validate("6281234567890"); err != nil {
		t.Errorf("call after window expiry rejected: %v", err)
	}
}

func TestRateLimitZeroMeansUnlimited(t *testing.T) {
	m := testManager(t, config.OutgoingConfig{
		Allowlist:       false,
		MaxCallsPerHour: 0,
	})
	for i := 0; i < 100; i++ {
		if err := m.Validate("6281234567890"); err != nil {
			t.Fatalf("call %d rejected with unlimited rate: %v", i+1, err)
		}
		m.recordCall(time.Now())
	}
}

func TestClassifyOutcome(t *testing.T) {
	cases := map[string]string{
		"":              eventspool.OutcomeCompleted,
		"completed":     eventspool.OutcomeCompleted,
		"ended":         eventspool.OutcomeCompleted,
		"no-answer":     eventspool.OutcomeNoAnswer,
		"no answer":     eventspool.OutcomeNoAnswer,
		"timeout":       eventspool.OutcomeNoAnswer,
		"unanswered":    eventspool.OutcomeNoAnswer,
		"busy":          eventspool.OutcomeBusy,
		"peer busy":     eventspool.OutcomeBusy,
		"transport error": eventspool.OutcomeFailed,
		"weird thing":   eventspool.OutcomeFailed,
	}
	for reason, want := range cases {
		if got := classifyOutcome(reason); got != want {
			t.Errorf("classifyOutcome(%q) = %q, want %q", reason, got, want)
		}
	}
}
