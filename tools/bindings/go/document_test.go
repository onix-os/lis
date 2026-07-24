package lis

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseExamples(t *testing.T) {
	examplesDir := filepath.Join("..", "..", "..", "docs", "examples")
	entries := []string{"server-btrfs.lis.json", "server-lvm-pool.lis.json"}

	for _, name := range entries {
		path := filepath.Join(examplesDir, name)
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("Failed to read example %s: %v", name, err)
		}

		doc, err := ParseJSON(data)
		if err != nil {
			t.Fatalf("Failed to parse example %s: %v", name, err)
		}

		if doc.LIS == "" {
			t.Fatalf("Expected LIS version in %s", name)
		}

		out, err := doc.ToJSON()
		if err != nil {
			t.Fatalf("Failed to serialize example %s: %v", name, err)
		}

		if len(out) == 0 {
			t.Fatalf("Serialized JSON is empty for %s", name)
		}
	}
}
