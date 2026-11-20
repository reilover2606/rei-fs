#include "api.hpp"

#include <openssl/evp.h>

#include <vector>

#ifndef CPPHTTPLIB_OPENSSL_SUPPORT
#define CPPHTTPLIB_OPENSSL_SUPPORT
#endif
#include <httplib.h>

namespace reifs {

std::string fingerprint_of(const unsigned char* der, std::size_t size) {
  unsigned char md[EVP_MAX_MD_SIZE];
  unsigned int len = 0;
  if (EVP_Digest(der, size, md, &len, EVP_sha256(), nullptr) != 1) return "";
  static const char* hex = "0123456789ABCDEF";
  std::string out;
  for (unsigned int i = 0; i < len; ++i) {
    if (i) out += ':';
    out += hex[md[i] >> 4];
    out += hex[md[i] & 15];
  }
  return out;
}

Api::Api(std::string host, int port, std::string pinned)
    : host_(std::move(host)), port_(port), pinned_(std::move(pinned)) {}

Api::~Api() = default;

void Api::ensure_client() {
  if (client_) return;
  client_ = std::make_unique<httplib::SSLClient>(host_, port_);
  client_->set_connection_timeout(5);
  client_->set_read_timeout(60);
  client_->set_write_timeout(30);
  client_->set_keep_alive(true);
  // Identity is established by the pinned fingerprint only: no CA, no host
  // name. The verifier always decides, so the built-in checks never run.
  client_->enable_server_hostname_verification(false);
  client_->set_session_verifier([this](httplib::tls::session_t session) {
    auto cert = httplib::tls::get_peer_cert(session);
    if (!cert) return httplib::SSLVerifierResponse::CertificateRejected;
    std::vector<unsigned char> der;
    bool ok = httplib::tls::get_cert_der(cert, der);
    httplib::tls::free_cert(cert);
    if (!ok) return httplib::SSLVerifierResponse::CertificateRejected;
    seen_ = fingerprint_of(der.data(), der.size());
    return (!pinned_.empty() && seen_ == pinned_)
               ? httplib::SSLVerifierResponse::CertificateAccepted
               : httplib::SSLVerifierResponse::CertificateRejected;
  });
}

std::string Api::probe() {
  std::lock_guard lock(mutex_);
  ensure_client();
  seen_.clear();
  auto res = client_->Get("/api/status");
  if (!res) {
    if (res.error() == httplib::Error::SSLServerVerification && !seen_.empty())
      throw UntrustedCertificate(seen_);
    throw ApiError(0, "cannot connect to " + host_ + ":" + std::to_string(port_) + ": " +
                          httplib::to_string(res.error()));
  }
  return seen_;
}

void Api::set_pinned(const std::string& fingerprint) {
  std::lock_guard lock(mutex_);
  pinned_ = fingerprint;
  client_.reset();  // reconnect with the new pin
}

void Api::login(const std::string& password) {
  auto out = post("/api/login", json{{"password", password}});
  std::lock_guard lock(mutex_);
  token_ = out.value("token", "");
  server_name_ = out.contains("server") ? out["server"].value("name", "") : "";
  server_id_ = out.contains("server") ? out["server"].value("id", "") : "";
}

void Api::logout() {
  try {
    post("/api/logout");
  } catch (const std::exception&) {
  }
  std::lock_guard lock(mutex_);
  token_.clear();
}

std::string Api::server_id() const {
  std::lock_guard lock(mutex_);
  return server_id_;
}

std::string Api::server_name() const {
  std::lock_guard lock(mutex_);
  return server_name_;
}

json Api::get(const std::string& path) { return request("GET", path, nullptr); }

json Api::post(const std::string& path, const json& body) { return request("POST", path, &body); }

json Api::request(const std::string& method, const std::string& path, const json* body) {
  std::lock_guard lock(mutex_);
  ensure_client();
  httplib::Headers headers;
  if (!token_.empty()) headers.emplace("Authorization", "Bearer " + token_);
  httplib::Result res = method == "GET"
                            ? client_->Get(path, headers)
                            : client_->Post(path, headers, body->dump(), "application/json");
  if (!res) {
    auto err = res.error();
    client_.reset();  // start over with a fresh connection next time
    if (err == httplib::Error::SSLServerVerification)
      throw UntrustedCertificate(seen_);
    throw ApiError(0, "connection error: " + httplib::to_string(err));
  }
  json payload;
  try {
    payload = json::parse(res->body);
  } catch (const json::exception&) {
    throw ApiError(res->status, "invalid response from server (HTTP " +
                                    std::to_string(res->status) + ")");
  }
  if (res->status >= 400) {
    std::string msg = payload.is_object() ? payload.value("error", "") : "";
    throw ApiError(res->status, msg.empty() ? "HTTP " + std::to_string(res->status) : msg);
  }
  return payload;
}

}  // namespace reifs
