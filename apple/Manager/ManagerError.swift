import OpenWeirManagerHeader

/// Why a call failed: `result` is its OPENWEIR_MANAGER_ code from OpenWeirManager.h.
struct ManagerError: Error {
  let result: Int
  let message: String

  static func failed(_ message: String) -> ManagerError {
    ManagerError(result: OPENWEIR_MANAGER_FAILED, message: message)
  }

  static func timedOut(_ message: String) -> ManagerError {
    ManagerError(result: OPENWEIR_MANAGER_TIMED_OUT, message: message)
  }

  static let notConfigured = ManagerError(
    result: OPENWEIR_MANAGER_NOT_CONFIGURED, message: "OpenWeir has no VPN configuration yet")

  static let notConnected = ManagerError(
    result: OPENWEIR_MANAGER_NOT_CONNECTED, message: "The OpenWeir tunnel is not connected")

  static let needsApproval = ManagerError(
    result: OPENWEIR_MANAGER_NEEDS_APPROVAL,
    message: "Allow the OpenWeir network extension in System Settings")

  static let needsReboot = ManagerError(
    result: OPENWEIR_MANAGER_NEEDS_REBOOT,
    message: "macOS has not completed the system extension request")

  static let busy = ManagerError(
    result: OPENWEIR_MANAGER_BUSY, message: "Another system extension request is still pending")

  static let cancelled = ManagerError(
    result: OPENWEIR_MANAGER_CANCELLED, message: "The system extension request was cancelled")

  static func notSignedIn(_ message: String) -> ManagerError {
    ManagerError(result: OPENWEIR_MANAGER_NOT_SIGNED_IN, message: message)
  }

  static func notFound(_ message: String) -> ManagerError {
    ManagerError(result: OPENWEIR_MANAGER_NOT_FOUND, message: message)
  }

  static func quotaExceeded(_ message: String) -> ManagerError {
    ManagerError(result: OPENWEIR_MANAGER_QUOTA_EXCEEDED, message: message)
  }

  static func unreachable(_ message: String) -> ManagerError {
    ManagerError(result: OPENWEIR_MANAGER_UNREACHABLE, message: message)
  }
}
