package incoming

import "testing"

func TestAllowed_ExactJID(t *testing.T) {
	list := []string{"66984377057451@lid", "6281234567890@s.whatsapp.net"}
	if !Allowed(list, "66984377057451@lid", "+6287899303065") {
		t.Error("expected LID JID to match")
	}
	if !Allowed(list, "6281234567890@s.whatsapp.net", "+6281234567890") {
		t.Error("expected s.whatsapp.net JID to match")
	}
	if Allowed(list, "6289999999999@s.whatsapp.net", "+6289999999999") {
		t.Error("expected unknown JID to be rejected")
	}
}

func TestAllowed_PhoneForms(t *testing.T) {
	list := []string{"+6281234567890"}
	if !Allowed(list, "6281234567890@s.whatsapp.net", "+6281234567890") {
		t.Error("expected phone with + to match entry with +")
	}
	if !Allowed(list, "6281234567890@s.whatsapp.net", "6281234567890") {
		t.Error("expected phone without + to match entry with +")
	}
	list = []string{"6281234567890"}
	if !Allowed(list, "6281234567890@s.whatsapp.net", "+6281234567890") {
		t.Error("expected phone with + to match entry without +")
	}
}

func TestAllowed_EmptyListStrict(t *testing.T) {
	if Allowed(nil, "6281234567890@s.whatsapp.net", "+6281234567890") {
		t.Error("expected empty allowlist to reject everyone (strict mode)")
	}
	if Allowed([]string{}, "6281234567890@s.whatsapp.net", "+6281234567890") {
		t.Error("expected empty allowlist to reject everyone (strict mode)")
	}
}

func TestAllowed_EmptyEntriesSkipped(t *testing.T) {
	list := []string{"", "  ", "+6281234567890"}
	if !Allowed(list, "6281234567890@s.whatsapp.net", "+6281234567890") {
		t.Error("expected blank entries to be skipped and real entry to match")
	}
}

func TestAllowed_UnresolvedPhone(t *testing.T) {
	// Caller JID unknown + phone unresolved → only exact JID entries can match.
	list := []string{"66984377057451@lid"}
	if !Allowed(list, "66984377057451@lid", "") {
		t.Error("expected JID match even when phone is unresolved")
	}
	if Allowed(list, "6281234567890@s.whatsapp.net", "") {
		t.Error("expected unresolved phone without JID match to be rejected")
	}
}
