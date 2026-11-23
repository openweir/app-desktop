//go:build (windows || linux) && (!with_naive || !with_purego)

package main

func loadNativeRuntime(string) error { return nil }
