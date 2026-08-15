package config

import (
	"fmt"
	"os"
	"path/filepath"
	"time"

	"gopkg.in/yaml.v3"
)

// Config holds all configuration for the MEOWcaller POC service.
type Config struct {
	WhatsApp  WhatsAppConfig  `yaml:"whatsapp"`
	Recording RecordingConfig `yaml:"recording"`
	Playback  PlaybackConfig  `yaml:"playback"`
	Server    ServerConfig    `yaml:"server"`
	Bridge    BridgeConfig    `yaml:"bridge"`
	Outgoing  OutgoingConfig  `yaml:"outgoing"`
	Incoming  IncomingConfig  `yaml:"incoming"`
}

type WhatsAppConfig struct {
	// StorePath is the path to the whatsmeow SQLite database.
	StorePath string `yaml:"store_path"`
	// DeviceName identifies this device.
	DeviceName string `yaml:"device_name"`
}

type RecordingConfig struct {
	// BaseDir is where call recordings are stored.
	BaseDir string `yaml:"base_dir"`
	// MaxCallDuration is the maximum allowed call duration before auto-hangup.
	MaxCallDuration time.Duration `yaml:"max_call_duration"`
	// RetentionDays is how many days to keep recordings.
	RetentionDays int `yaml:"retention_days"`
}

// PlaybackConfig controls the auto-playback feature.
type PlaybackConfig struct {
	// File is the path to a WAV file to play to the caller after auto-answer.
	// Empty string disables playback.
	File string `yaml:"file"`
	// HangupAfterPlay causes the call to hang up once playback finishes.
	// If false, the call stays open for the caller to speak.
	HangupAfterPlay bool `yaml:"hangup_after_play"`
}

type ServerConfig struct {
	// LogFile is the path to the log file.
	LogFile string `yaml:"log_file"`
	// LogLevel is the logging level (debug, info, warn, error).
	LogLevel string `yaml:"log_level"`
}

// BridgeConfig controls the WebSocket audio bridge for external agent connections.
type BridgeConfig struct {
	// Enabled turns on the WebSocket bridge server.
	Enabled bool `yaml:"enabled"`
	// Listen is the address to bind the WebSocket server (e.g. "127.0.0.1:9090").
	Listen string `yaml:"listen"`
	// Path is the WebSocket URL path (e.g. "/ws").
	Path string `yaml:"path"`
}

// OutgoingConfig controls outbound call placement (the /api/call endpoint).
type OutgoingConfig struct {
	// Enabled turns on the outgoing call API. When false, /api/call returns 503.
	Enabled bool `yaml:"enabled"`
	// Allowlist toggles the allowlist guard. true (default) = only phone
	// numbers found in the OpenClaw sessions store (SessionStorePath, i.e.
	// numbers that have an active/direct chat session) can be called; false =
	// any valid E.164 number can be called with no check at all.
	Allowlist bool `yaml:"allowlist"`
	// MaxCallsPerHour caps outbound calls per rolling hour. 0 = unlimited (not recommended).
	MaxCallsPerHour int `yaml:"max_calls_per_hour"`
	// DefaultDelayMs is the silence before playing audio after the peer answers.
	DefaultDelayMs int `yaml:"default_delay_ms"`
	// RingTimeout is how long to wait for the peer to answer before hanging up.
	RingTimeout time.Duration `yaml:"ring_timeout"`
	// AudioDir is the default directory for announcement WAV files. Relative
	// audio paths in API requests are resolved against this directory.
	AudioDir string `yaml:"audio_dir"`
	// SessionStorePath is the OpenClaw sessions.json used for allowlisting:
	// phone numbers found in it (direct chat sessions) are the callable set
	// when Allowlist is true. Empty string disables the feature (no numbers
	// will pass the allowlist check).
	SessionStorePath string `yaml:"session_store_path"`
	// SessionAllowlistTTL caches the parsed session-derived allowlist for this
	// long before re-reading the file. Defaults to 60s when zero.
	SessionAllowlistTTL time.Duration `yaml:"session_allowlist_ttl"`
	// HangupAfterPlaySec keeps the call alive this many seconds after the
	// announcement WAV finishes playing before hanging up. The total call time
	// from play start is WAV duration + this value.
	HangupAfterPlaySec int `yaml:"hangup_after_play_sec"`
}

// IncomingConfig controls inbound call answering.
type IncomingConfig struct {
	// Allowlist enables the incoming-call allowlist guard. When true, only
	// callers whose JID or resolved phone number appears in AllowlistNumbers
	// are answered; every other caller is rejected (call.Reject()) before any
	// media setup. When false (default), all incoming calls are answered as
	// before — backward compatible.
	Allowlist bool `yaml:"allowlist"`
	// AllowlistNumbers is the set of allowed callers. Entries may be full JIDs
	// ("66984377057451@lid", "6281234567890@s.whatsapp.net") or phone numbers
	// with or without the "+" prefix ("+6281234567890", "6281234567890").
	// An empty list with Allowlist=true rejects every caller (strict mode).
	AllowlistNumbers []string `yaml:"allowlist_numbers"`
}

// DefaultConfig returns a Config with sensible defaults.
func DefaultConfig() *Config {
	return &Config{
		WhatsApp: WhatsAppConfig{
			StorePath:  filepath.Join(os.Getenv("HOME"), ".meowcaller", "store.db"),
			DeviceName: "meowcaller-poc",
		},
		Recording: RecordingConfig{
			BaseDir:         "/var/lib/meowcaller/calls",
			MaxCallDuration: 10 * time.Minute,
			RetentionDays:   30,
		},
		Server: ServerConfig{
			LogFile:  "/var/log/meowcaller/poc.log",
			LogLevel: "info",
		},
		Bridge: BridgeConfig{
			Enabled: false,
			Listen:  "127.0.0.1:9090",
			Path:    "/ws",
		},
		Outgoing: OutgoingConfig{
			Enabled:              false,
			Allowlist:            true,
			MaxCallsPerHour:      10,
			DefaultDelayMs:       2000,
			RingTimeout:          60 * time.Second,
			AudioDir:             "",
			SessionAllowlistTTL:  60 * time.Second,
			HangupAfterPlaySec:   3,
		},
	}
}

// LoadConfig reads a YAML config file, falling back to defaults for missing fields.
func LoadConfig(path string) (*Config, error) {
	cfg := DefaultConfig()
	if path == "" {
		return cfg, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}
	if err := yaml.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	return cfg, nil
}
