// Client settings: last server used and pinned certificate fingerprints.
#pragma once

#include <filesystem>
#include <map>
#include <string>

namespace reifs {

struct Profiles {
  std::string last_host = "localhost";
  int last_port = 7420;
  // "host:port" -> SHA-256 fingerprint of the server certificate.
  std::map<std::string, std::string> known_servers;

  static std::filesystem::path config_dir();
  static std::filesystem::path config_file();
  static Profiles load();
  void save() const;

  static std::string key(const std::string& host, int port);
};

}  // namespace reifs
