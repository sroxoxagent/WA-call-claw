package main

import (
	"context"
	"testing"

	"github.com/meowcaller-poc/internal/incoming"
	"go.mau.fi/whatsmeow/types"
)

func TestResolvePeerPhone_DefaultUserServer(t *testing.T) {
	// Plain phone JID (6287899303065@s.whatsapp.net) → "+6287899303065".
	peer := types.NewJID("6287899303065", types.DefaultUserServer)
	got := resolvePeerPhone(context.Background(), nil, peer)
	if got != "+6287899303065" {
		t.Errorf("expected +6287899303065, got %q", got)
	}
}

func TestResolvePeerPhone_EmptyPeer(t *testing.T) {
	if got := resolvePeerPhone(context.Background(), nil, types.EmptyJID); got != "" {
		t.Errorf("expected empty for empty JID, got %q", got)
	}
}

func TestResolvePeerPhone_NilDevice(t *testing.T) {
	// LID resolution requires a device; nil device must not panic and returns "".
	peer := types.NewJID("66984377057451", types.HiddenUserServer)
	if got := resolvePeerPhone(context.Background(), nil, peer); got != "" {
		t.Errorf("expected empty for nil device LID, got %q", got)
	}
}

func TestResolvePeerPhone_AllowlistRoundTrip(t *testing.T) {
	// The full path: caller arrives as LID → resolved to phone → allowlist
	// entry is a plain phone number. This mirrors the real guard flow:
	//   incoming.Allowed(list, peerJID, resolvePeerPhone(...))
	// with the phone-only allowlist Shendy requested (2026-08-16).
	peer := types.NewJID("6287899303065", types.DefaultUserServer)
	phone := resolvePeerPhone(context.Background(), nil, peer)
	list := []string{"+6287899303065", "+66986575115"}
	if !incoming.Allowed(list, peer.String(), phone) {
		t.Errorf("expected phone %q to be allowed by list %v", phone, list)
	}
	other := types.NewJID("6289999999999", types.DefaultUserServer)
	if incoming.Allowed(list, other.String(), resolvePeerPhone(context.Background(), nil, other)) {
		t.Errorf("expected unknown phone to be rejected")
	}
}
