// Package frame encodes the 12-byte header used on the Windows service pipe
// (docs/architecture.md §4.2): magic, u16 version, u16 kind, u32 payload length,
// all little-endian. macOS provider messages carry the payload without a header.
package frame

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"

	"github.com/openweir/app-desktop/src/core/internal/product"
)

type Kind uint16

const (
	KindRequest  Kind = 1
	KindResponse Kind = 2
)

const (
	HeaderSize       = 12
	MaxRequestBytes  = 16 << 20
	MaxResponseBytes = 8 << 20
)

var ErrInvalidHeader = errors.New("invalid control frame header")

func limit(kind Kind) int {
	if kind == KindRequest {
		return MaxRequestBytes
	}
	return MaxResponseBytes
}

// Write sends one frame. The payload must already be within the kind's limit.
func Write(w io.Writer, kind Kind, payload []byte) error {
	if len(payload) == 0 || len(payload) > limit(kind) {
		return fmt.Errorf("control frame payload of %d bytes is out of range", len(payload))
	}
	frame := make([]byte, HeaderSize+len(payload))
	copy(frame, product.FrameMagic[:])
	binary.LittleEndian.PutUint16(frame[4:], product.ProtocolVersion)
	binary.LittleEndian.PutUint16(frame[6:], uint16(kind))
	binary.LittleEndian.PutUint32(frame[8:], uint32(len(payload)))
	copy(frame[HeaderSize:], payload)
	_, err := w.Write(frame)
	return err
}

// Read receives one frame of the expected kind. The header is validated before the
// payload buffer is allocated, so an oversized length never reaches make.
func Read(r io.Reader, want Kind) ([]byte, error) {
	length, err := ReadHeader(r, want)
	if err != nil {
		return nil, err
	}
	return ReadPayload(r, length)
}

// ReadHeader receives and validates one header, and returns the length of the payload
// that follows it. A reader with a lower limit of its own reads the header with this.
func ReadHeader(r io.Reader, want Kind) (int, error) {
	var header [HeaderSize]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		return 0, err
	}
	return ParseHeader(header, want)
}

// ReadPayload receives the length bytes of payload that follow a header.
func ReadPayload(r io.Reader, length int) ([]byte, error) {
	payload := make([]byte, length)
	if _, err := io.ReadFull(r, payload); err != nil {
		return nil, unexpectedEOF(err)
	}
	return payload, nil
}

// Skip reads past length bytes of payload without keeping them, so the next frame can be
// read. It returns the first prefix of them, at most what prefix holds.
func Skip(r io.Reader, length int, prefix []byte) ([]byte, error) {
	prefix = prefix[:min(len(prefix), length)]
	if _, err := io.ReadFull(r, prefix); err != nil {
		return nil, unexpectedEOF(err)
	}
	if _, err := io.CopyN(io.Discard, r, int64(length-len(prefix))); err != nil {
		return nil, unexpectedEOF(err)
	}
	return prefix, nil
}

func unexpectedEOF(err error) error {
	if errors.Is(err, io.EOF) {
		return io.ErrUnexpectedEOF
	}
	return err
}

func ParseHeader(header [HeaderSize]byte, want Kind) (int, error) {
	if [4]byte(header[:4]) != product.FrameMagic {
		return 0, fmt.Errorf("%w: bad magic", ErrInvalidHeader)
	}
	if version := binary.LittleEndian.Uint16(header[4:]); version != product.ProtocolVersion {
		return 0, fmt.Errorf("%w: protocol version %d, want %d", ErrInvalidHeader, version, product.ProtocolVersion)
	}
	if kind := Kind(binary.LittleEndian.Uint16(header[6:])); kind != want {
		return 0, fmt.Errorf("%w: kind %d, want %d", ErrInvalidHeader, kind, want)
	}
	length := binary.LittleEndian.Uint32(header[8:])
	if length == 0 || uint64(length) > uint64(limit(want)) {
		return 0, fmt.Errorf("%w: payload length %d", ErrInvalidHeader, length)
	}
	return int(length), nil
}
