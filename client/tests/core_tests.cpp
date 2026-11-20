// Unit tests for the client core. With REIFS_TEST_PORT and
// REIFS_TEST_PASSWORD set (and a server on localhost), also exercises
// certificate pinning and login against a live server.
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <string>

#include "../src/api.hpp"
#include "../src/format.hpp"
#include "../src/profiles.hpp"

static int failures = 0;

#define CHECK(cond)                                                      \
  do {                                                                   \
    if (!(cond)) {                                                       \
      std::cerr << __FILE__ << ":" << __LINE__ << ": FAILED: " #cond "\n"; \
      ++failures;                                                        \
    }                                                                    \
  } while (0)

static void test_format() {
  CHECK(reifs::human_size(0) == "0 B");
  CHECK(reifs::human_size(1023) == "1023 B");
  CHECK(reifs::human_size(1536) == "1.5 KiB");
  CHECK(reifs::human_size(5LL * 1024 * 1024 * 1024) == "5.0 GiB");
  CHECK(reifs::url_encode("/a b/c?d&e") == "/a%20b/c%3Fd%26e");
  CHECK(reifs::fit("abc", 5) == "abc  ");
  CHECK(reifs::fit("abcdefgh", 5) == "abcd~");
  CHECK(reifs::format_time(0).empty());
}

static void test_fingerprint() {
  const unsigned char data[] = {'a', 'b', 'c'};
  // SHA-256("abc")
  CHECK(reifs::fingerprint_of(data, 3) ==
        "BA:78:16:BF:8F:01:CF:EA:41:41:40:DE:5D:AE:22:23:"
        "B0:03:61:A3:96:17:7A:9C:B4:10:FF:61:F2:00:15:AD");
}

static void test_profiles() {
  auto dir = std::filesystem::temp_directory_path() / "reifs-client-test";
  std::filesystem::remove_all(dir);
#ifdef _WIN32
  _putenv_s("REIFS_CLIENT_HOME", dir.string().c_str());
#else
  setenv("REIFS_CLIENT_HOME", dir.string().c_str(), 1);
#endif
  auto p = reifs::Profiles::load();
  CHECK(p.last_port == 7420);
  p.last_host = "nas";
  p.known_servers[reifs::Profiles::key("nas", 7420)] = "AA:BB";
  p.save();
  auto q = reifs::Profiles::load();
  CHECK(q.last_host == "nas");
  CHECK(q.known_servers.at("nas:7420") == "AA:BB");
  std::filesystem::remove_all(dir);
}

static void test_live() {
  const char* port = std::getenv("REIFS_TEST_PORT");
  const char* password = std::getenv("REIFS_TEST_PASSWORD");
  if (!port || !password) {
    std::cout << "live server tests skipped (set REIFS_TEST_PORT, REIFS_TEST_PASSWORD)\n";
    return;
  }
  reifs::Api api("127.0.0.1", std::atoi(port));
  std::string seen;
  try {
    api.probe();
    CHECK(false);  // nothing pinned: must be refused
  } catch (const reifs::UntrustedCertificate& e) {
    seen = e.seen;
  }
  CHECK(seen.size() == 95);
  if (const char* expected = std::getenv("REIFS_TEST_FINGERPRINT")) CHECK(seen == expected);

  reifs::Api wrong("127.0.0.1", std::atoi(port), std::string(95, 'A'));
  try {
    wrong.probe();
    CHECK(false);  // mismatching pin must be refused
  } catch (const reifs::UntrustedCertificate& e) {
    CHECK(e.seen == seen);
  }

  api.set_pinned(seen);
  CHECK(api.probe() == seen);
  try {
    api.login("definitely wrong");
    CHECK(false);
  } catch (const reifs::ApiError& e) {
    CHECK(e.status == 401);
  }
  api.login(password);
  auto status = api.get("/api/status");
  CHECK(status.contains("files"));
  auto tree = api.get("/api/tree?path=/");
  CHECK(tree["entries"].is_array());
  api.logout();
  try {
    api.get("/api/status");
    CHECK(false);
  } catch (const reifs::ApiError& e) {
    CHECK(e.status == 401);
  }
  std::cout << "live server tests passed\n";
}

int main() {
  test_format();
  test_fingerprint();
  test_profiles();
  test_live();
  if (failures) {
    std::cerr << failures << " failure(s)\n";
    return 1;
  }
  std::cout << "all core tests passed\n";
  return 0;
}
