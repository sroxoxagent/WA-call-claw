package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadConfigWithPlayback(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "test.yaml")

	yaml := `
whatsapp:
  store_path: "/tmp/test.db"
  device_name: "test-device"
recording:
  base_dir: "/tmp/calls"
  max_call_duration: "5m"
  retention_days: 7
playback:
  file: "/tmp/test.wav"
  hangup_after_play: true
server:
  log_file: "/tmp/test.log"
  log_level: "debug"
`
	if err := os.WriteFile(configPath, []byte(yaml), 0644); err != nil {
		t.Fatalf("write test config: %v", err)
	}

	cfg, err := LoadConfig(configPath)
	if err != nil {
		t.Fatalf("LoadConfig failed: %v", err)
	}

	if cfg.WhatsApp.StorePath != "/tmp/test.db" {
		t.Errorf("expected StorePath=/tmp/test.db, got %s", cfg.WhatsApp.StorePath)
	}
	if cfg.Playback.File != "/tmp/test.wav" {
		t.Errorf("expected Playback.File=/tmp/test.wav, got %s", cfg.Playback.File)
	}
	if !cfg.Playback.HangupAfterPlay {
		t.Error("expected HangupAfterPlay=true")
	}
	if cfg.Recording.MaxCallDuration != 5*60*1000*1000*1000 { // 5m in nanoseconds
		t.Errorf("unexpected MaxCallDuration: %v", cfg.Recording.MaxCallDuration)
	}
}

func TestLoadConfigPlaybackDefaults(t *testing.T) {
	cfg := DefaultConfig()

	if cfg.Playback.File != "" {
		t.Errorf("expected empty Playback.File by default, got %s", cfg.Playback.File)
	}
	if cfg.Playback.HangupAfterPlay {
		t.Error("expected HangupAfterPlay=false by default")
	}
}

func TestLoadConfigEmptyPlayback(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "test.yaml")

	yaml := `
whatsapp:
  store_path: "/tmp/test.db"
recording:
  base_dir: "/tmp/calls"
`
	if err := os.WriteFile(configPath, []byte(yaml), 0644); err != nil {
		t.Fatalf("write test config: %v", err)
	}

	cfg, err := LoadConfig(configPath)
	if err != nil {
		t.Fatalf("LoadConfig failed: %v", err)
	}

	if cfg.Playback.File != "" {
		t.Errorf("expected empty Playback.File when not specified, got %s", cfg.Playback.File)
	}
}

func TestLoadConfigBridgeDefaults(t *testing.T) {
	cfg := DefaultConfig()

	if cfg.Bridge.Enabled {
		t.Error("expected Bridge.Enabled=false by default")
	}
	if cfg.Bridge.Listen != "127.0.0.1:9090" {
		t.Errorf("expected Bridge.Listen=127.0.0.1:9090, got %s", cfg.Bridge.Listen)
	}
	if cfg.Bridge.Path != "/ws" {
		t.Errorf("expected Bridge.Path=/ws, got %s", cfg.Bridge.Path)
	}
}

func TestLoadConfigBridgeEnabled(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "test.yaml")

	yaml := `
whatsapp:
  store_path: "/tmp/test.db"
recording:
  base_dir: "/tmp/calls"
bridge:
  enabled: true
  listen: "0.0.0.0:8080"
  path: "/agent"
`
	if err := os.WriteFile(configPath, []byte(yaml), 0644); err != nil {
		t.Fatalf("write test config: %v", err)
	}

	cfg, err := LoadConfig(configPath)
	if err != nil {
		t.Fatalf("LoadConfig failed: %v", err)
	}

	if !cfg.Bridge.Enabled {
		t.Error("expected Bridge.Enabled=true")
	}
	if cfg.Bridge.Listen != "0.0.0.0:8080" {
		t.Errorf("expected Bridge.Listen=0.0.0.0:8080, got %s", cfg.Bridge.Listen)
	}
	if cfg.Bridge.Path != "/agent" {
		t.Errorf("expected Bridge.Path=/agent, got %s", cfg.Bridge.Path)
	}
}
