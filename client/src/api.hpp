// HTTPS client for the rei-fs server API with certificate pinning.
//
// The server uses a self-signed certificate. The client pins the SHA-256
// fingerprint of that certificate the first time it connects (like SSH);
// any later mismatch is refused.
#pragma once

#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>

#include <nlohmann/json.hpp>

namespace httplib {
class SSLClient;
}

namespace reifs {

using json = nlohmann::json;

struct ApiError : std::runtime_error {
  int status;  // HTTP status, or 0 for connection problems
  ApiError(int s, const std::string& msg) : std::runtime_error(msg), status(s) {}
};

// The server's certificate does not match the pinned fingerprint (or none is
// pinned yet). `seen` is what the server presented.
struct UntrustedCertificate : std::runtime_error {
  std::string seen;
  explicit UntrustedCertificate(std::string fp)
      : std::runtime_error("untrusted server certificate"), seen(std::move(fp)) {}
};

// "AB:CD:..." SHA-256 fingerprint of a DER certificate.
std::string fingerprint_of(const unsigned char* der, std::size_t size);

class Api {
 public:
  Api(std::string host, int port, std::string pinned_fingerprint = "");
  ~Api();
  Api(const Api&) = delete;
  Api& operator=(const Api&) = delete;

  // Connects without credentials. Returns the server's fingerprint if it
  // matches the pin; throws UntrustedCertificate otherwise.
  std::string probe();
  void set_pinned(const std::string& fingerprint);
  // Throws ApiError on a wrong password.
  void login(const std::string& password);
  void logout();

  json get(const std::string& path);
  json post(const std::string& path, const json& body = json::object());

  std::string server_name() const;
  std::string host() const { return host_; }
  int port() const { return port_; }

 private:
  json request(const std::string& method, const std::string& path, const json* body);
  void ensure_client();

  std::string host_;
  int port_;
  std::string pinned_;
  std::string seen_;
  std::string token_;
  std::string server_name_;
  std::unique_ptr<httplib::SSLClient> client_;
  mutable std::mutex mutex_;
};

}  // namespace reifs
