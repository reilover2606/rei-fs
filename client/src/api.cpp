#include "api.hpp"

#include <openssl/evp.h>

#include <chrono>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <vector>

#ifndef CPPHTTPLIB_OPENSSL_SUPPORT
#define CPPHTTPLIB_OPENSSL_SUPPORT
#endif
#include <httplib.h>

namespace reifs {

namespace {

// Optional request log for diagnostics: REIFS_CLIENT_LOG=/path/to/file.
void log_request(const std::string& method, const std::string& path, int status,
                 double seconds, const std::string& note) {
  static const char* file = std::getenv("REIFS_CLIENT_LOG");
  if (!file || !*file) return;
  static std::mutex mutex;
  std::lock_guard lock(mutex);
  std::ofstream out(file, std::ios::app);
  std::time_t now = std::time(nullptr);
  char stamp[32];
  std::strftime(stamp, sizeof stamp, "%H:%M:%S", std::localtime(&now));
  out << stamp << " " << method << " " << path << " -> " << status << " in " << seconds
      << " s" << (note.empty() ? "" : " (" + note + ")") << "\n";
}

}  // namespace

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
    : host_(std::move(host)), port_(port), session_(std::make_shared<Session>()) {
  session_->pinned = std::move(pinned);
}

std::unique_ptr<Api> Api::spawn() const {
  auto other = std::make_unique<Api>(host_, port_);
  other->session_ = session_;
  return other;
}

std::string Api::pinned() const {
  std::lock_guard lock(session_->mutex);
  return session_->pinned;
}

std::string Api::token() const {
  std::lock_guard lock(session_->mutex);
  return session_->token;
}

Api::~Api() = default;

void Api::ensure_client() {
  if (client_) return;
  client_ = std::make_unique<httplib::SSLClient>(host_, port_);
  client_->set_connection_timeout(5);
  client_->set_read_timeout(read_timeout_);
  client_->set_write_timeout(30);
  client_->set_keep_alive(true);
  {
    std::lock_guard lock(abort_mutex_);
    live_ = client_.get();
  }
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
    std::string pin = pinned();
    return (!pin.empty() && seen_ == pin)
               ? httplib::SSLVerifierResponse::CertificateAccepted
               : httplib::SSLVerifierResponse::CertificateRejected;
  });
}

void Api::drop_client() {
  {
    std::lock_guard lock(abort_mutex_);
    live_ = nullptr;
  }
  client_.reset();
}

void Api::abort() {
  std::lock_guard lock(abort_mutex_);
  if (live_) live_->stop();   // shuts the socket down; the waiting request fails
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
  {
    std::lock_guard lock(session_->mutex);
    session_->pinned = fingerprint;
  }
  std::lock_guard lock(mutex_);
  drop_client();  // reconnect with the new pin
}

void Api::login(const std::string& password) {
  auto out = post("/api/login", json{{"password", password}});
  std::lock_guard lock(session_->mutex);
  session_->token = out.value("token", "");
  session_->server_name = out.contains("server") ? out["server"].value("name", "") : "";
  session_->server_id = out.contains("server") ? out["server"].value("id", "") : "";
}

void Api::logout() {
  try {
    post("/api/logout");
  } catch (const std::exception&) {
  }
  std::lock_guard lock(session_->mutex);
  session_->token.clear();
}

std::string Api::server_id() const {
  std::lock_guard lock(session_->mutex);
  return session_->server_id;
}

std::string Api::server_name() const {
  std::lock_guard lock(session_->mutex);
  return session_->server_name;
}

json Api::get(const std::string& path) { return request("GET", path, nullptr); }

json Api::post(const std::string& path, const json& body) { return request("POST", path, &body); }

json Api::request(const std::string& method, const std::string& path, const json* body) {
  auto started = std::chrono::steady_clock::now();
  auto elapsed = [&] {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  };
  std::lock_guard lock(mutex_);
  double waited_for_lock = elapsed();
  ensure_client();
  httplib::Headers headers;
  if (auto t = token(); !t.empty()) headers.emplace("Authorization", "Bearer " + t);
  httplib::Result res = method == "GET"
                            ? client_->Get(path, headers)
                            : client_->Post(path, headers, body->dump(), "application/json");
  if (!res) {
    auto err = res.error();
    log_request(method, path, 0, elapsed(), httplib::to_string(err));
    drop_client();    // start over with a fresh connection next time
    if (err == httplib::Error::SSLServerVerification)
      throw UntrustedCertificate(seen_);
    throw ApiError(0, "connection error: " + httplib::to_string(err));
  }
  log_request(method, path, res->status, elapsed(),
              std::to_string(res->body.size()) + " bytes" +
                  (waited_for_lock > 0.5 ? ", waited " + std::to_string(waited_for_lock) +
                                               " s for another request"
                                         : ""));
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
