// Disposable synthetic SecItem fixture; only statuses and match booleans leave the process.
import Foundation
import Security
SecKeychainSetUserInteractionAllowed(false)
let args = CommandLine.arguments
let path = args[2]
var kc: SecKeychain?
let mode = args[1]
if mode == "create" {
  let password = "synthetic-fixture-password"
  let status = password.withCString { SecKeychainCreate(path, UInt32(password.utf8.count), $0, false, nil, &kc) }
  guard status == errSecSuccess, let keychain = kc else { print("create status \(status)"); exit(1) }
  let item: [String: Any] = [kSecClass as String:kSecClassGenericPassword, kSecAttrService as String:"omni.synthetic.keychain", kSecAttrAccount as String:"synthetic", kSecValueData as String:Data("fixture-only-value".utf8), kSecUseKeychain as String:keychain]
  let added = SecItemAdd(item as CFDictionary, nil)
  print("create status \(added)")
  exit(added == errSecSuccess ? 0 : 1)
}
let opened = SecKeychainOpen(path, &kc)
guard opened == errSecSuccess, let keychain = kc else { print("open status \(opened)"); exit(1) }
if mode == "delete" { let status = SecKeychainDelete(keychain); print("delete status \(status)"); exit(status == errSecSuccess ? 0 : 1) }
let query: [String: Any] = [kSecClass as String:kSecClassGenericPassword, kSecAttrService as String:"omni.synthetic.keychain", kSecAttrAccount as String:"synthetic", kSecMatchSearchList as String:[keychain], kSecMatchLimit as String:kSecMatchLimitOne, kSecReturnData as String:true, kSecReturnAttributes as String:true]
var result: CFTypeRef?
let status = SecItemCopyMatching(query as CFDictionary, &result)
let matched = (result as? [String:Any])?[kSecValueData as String] as? Data == Data("fixture-only-value".utf8)
print("query status \(status) matched \(matched)")
exit(status == errSecSuccess && matched ? 0 : 1)
