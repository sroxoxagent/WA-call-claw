package whatsapp

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sync"

	_ "github.com/mattn/go-sqlite3"
	"github.com/purpshell/meowcaller"
	"github.com/rs/zerolog"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// Session manages the WhatsApp connection via whatsmeow + meowcaller.
type Session struct {
	storePath string
	device    *whatsmeow.Client
	client    *meowcaller.Client
	mu        sync.Mutex
}

// NewSession creates a new WhatsApp session from the given store path.
func NewSession(storePath string) (*Session, error) {
	s := &Session{storePath: storePath}
	if err := s.init(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Session) init() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Ensure the store directory exists.
	dir := filepath.Dir(s.storePath)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return fmt.Errorf("create store dir: %w", err)
	}

	ctx := context.Background()
	dbLog := waLog.Stdout("Database", "ERROR", true)

	dsn := fmt.Sprintf("file:%s?_foreign_keys=on", s.storePath)
	container, err := sqlstore.New(ctx, "sqlite3", dsn, dbLog)
	if err != nil {
		return fmt.Errorf("open whatsmeow store: %w", err)
	}

	deviceStore, err := container.GetFirstDevice(ctx)
	if err != nil {
		if err == sql.ErrNoRows {
			deviceStore = container.NewDevice()
		} else {
			return fmt.Errorf("get device: %w", err)
		}
	}

	logger := waLog.Stdout("WhatsApp", "ERROR", true)
	s.device = whatsmeow.NewClient(deviceStore, logger)
	// Engine diagnostics: surface relay/RTP/transport logs (library is silent by default).
	engineLog := zerolog.New(zerolog.ConsoleWriter{Out: os.Stdout, TimeFormat: "15:04:05"}).Level(zerolog.InfoLevel)
	s.client = meowcaller.NewClient(s.device, meowcaller.WithLogger(engineLog))
	return nil
}

// Connect establishes the WhatsApp connection. If not yet authenticated, it pairs via QR.
func (s *Session) Connect(ctx context.Context) error {
	s.mu.Lock()
	dev := s.device
	s.mu.Unlock()

	if dev == nil {
		return fmt.Errorf("session not initialized")
	}

	if dev.Store.ID == nil {
		// Not yet authenticated — need QR pairing.
		log.Println("WhatsApp: not authenticated, generating QR code for pairing...")
		ch, err := dev.GetQRChannel(ctx)
		if err != nil {
			return fmt.Errorf("get QR channel: %w", err)
		}
		go func() {
			for evt := range ch {
				if evt.Code != "" {
					fmt.Fprintf(os.Stderr, "Pairing QR code:\n%s\n", evt.Code)
				}
			}
		}()
	}

	if err := dev.Connect(); err != nil {
		return fmt.Errorf("connect: %w", err)
	}

	log.Println("WhatsApp: connected successfully")
	return nil
}

// OnIncomingCall registers a callback for incoming calls.
func (s *Session) OnIncomingCall(fn func(call *meowcaller.Call)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.client.OnIncomingCall(fn)
}

// Client returns the underlying meowcaller client.
func (s *Session) Client() *meowcaller.Client {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.client
}

// Device returns the underlying whatsmeow client.
func (s *Session) Device() *whatsmeow.Client {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.device
}

// Close disconnects from WhatsApp.
func (s *Session) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.device != nil {
		s.device.Disconnect()
	}
}
