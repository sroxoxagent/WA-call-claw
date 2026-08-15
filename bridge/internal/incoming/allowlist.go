// Package incoming guards inbound calls against an allowlist.
//
// The guard runs in handleIncomingCall BEFORE any media/recorder setup:
// a caller that is not allowlisted is left ringing — the bridge neither
// answers nor rejects, so other devices (e.g. the owner's phone) can
// still pick the call up.
package incoming

import "strings"

// Allowed reports whether a caller may be answered.
//
// Entries in the allowlist may be full JIDs ("66984377057451@lid",
// "6281234567890@s.whatsapp.net") or phone numbers with or without the "+"
// prefix ("+6281234567890", "6281234567890"). Matching is:
//   - exact JID equality, or
//   - phone-number equality after stripping the "+" prefix from both sides.
//
// An empty allowlist rejects every caller (strict mode).
func Allowed(allowlist []string, peerJID, peerPhone string) bool {
	if len(allowlist) == 0 {
		return false
	}
	normPhone := strings.TrimPrefix(strings.TrimSpace(peerPhone), "+")
	for _, entry := range allowlist {
		e := strings.TrimSpace(entry)
		if e == "" {
			continue
		}
		if e == peerJID {
			return true
		}
		if normPhone != "" && strings.TrimPrefix(e, "+") == normPhone {
			return true
		}
	}
	return false
}
