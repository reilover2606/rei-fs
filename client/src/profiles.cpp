#include "profiles.hpp"

#include <cstdlib>
#include <fstream>

#include <nlohmann/json.hpp>

namespace reifs {

namespace fs = std::filesystem;

fs::path Profiles::config_dir() {
  if (const char* env = std::getenv("REIFS_CLIENT_HOME"); env && *env) return fs::path(env);
#ifdef _WIN32
  if (const char* appdata = std::getenv("APPDATA"); appdata && *appdata)
    return fs::path(appdata) / "rei-fs-client";
#endif
  if (const char* xdg = std::getenv("XDG_CONFIG_HOME"); xdg && *xdg)
    return fs::path(xdg) / "rei-fs-client";
  if (const char* home = std::getenv("HOME"); home && *home)
    return fs::path(home) / ".config" / "rei-fs-client";
  return fs::path(".rei-fs-client");
}

fs::path Profiles::config_file() { return config_dir() / "client.json"; }

std::string Profiles::key(const std::string& host, int port) {
  return host + ":" + std::to_string(port);
}

Profiles Profiles::load() {
  Profiles p;
  std::ifstream in(config_file());
  if (!in) return p;
  try {
    auto j = nlohmann::json::parse(in);
    p.last_host = j.value("last_host", p.last_host);
    p.last_port = j.value("last_port", p.last_port);
    if (j.contains("known_servers") && j["known_servers"].is_object())
      p.known_servers = j["known_servers"].get<std::map<std::string, std::string>>();
  } catch (const std::exception&) {
    // A corrupt settings file is replaced on the next save.
  }
  return p;
}

void Profiles::save() const {
  fs::create_directories(config_dir());
  nlohmann::json j = {{"last_host", last_host},
                      {"last_port", last_port},
                      {"known_servers", known_servers}};
  auto tmp = config_file();
  tmp += ".tmp";
  {
    std::ofstream out(tmp, std::ios::trunc);
    out << j.dump(2) << '\n';
  }
  fs::rename(tmp, config_file());
}

}  // namespace reifs
