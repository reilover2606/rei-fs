#include "format.hpp"

#include <ctime>
#include <iomanip>
#include <sstream>

#include <ftxui/screen/string.hpp>

namespace reifs {

std::string human_size(std::int64_t bytes) {
  static const char* units[] = {"B", "KiB", "MiB", "GiB", "TiB"};
  double size = static_cast<double>(bytes);
  int unit = 0;
  while (size >= 1024.0 && unit < 4) {
    size /= 1024.0;
    ++unit;
  }
  std::ostringstream out;
  if (unit == 0) {
    out << bytes << " B";
  } else {
    out << std::fixed << std::setprecision(1) << size << ' ' << units[unit];
  }
  return out.str();
}

std::string format_time(std::int64_t seconds) {
  if (seconds <= 0) return "";
  std::time_t t = static_cast<std::time_t>(seconds);
  std::tm tm{};
#ifdef _WIN32
  localtime_s(&tm, &t);
#else
  localtime_r(&t, &tm);
#endif
  char buf[32];
  std::strftime(buf, sizeof buf, "%Y-%m-%d %H:%M", &tm);
  return buf;
}

std::string url_encode(const std::string& value) {
  static const char* hex = "0123456789ABCDEF";
  std::string out;
  // Every byte of a UTF-8 sequence is >= 0x80 and gets percent-encoded. The
  // ASCII test is spelled out: isalnum() depends on the locale and may accept
  // some bytes >= 0x80 (Latin-1 locales), which would corrupt CJK text.
  for (unsigned char c : value) {
    bool ascii_alnum = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
    if (ascii_alnum || c == '-' || c == '_' || c == '.' || c == '~' || c == '/') {
      out += static_cast<char>(c);
    } else {
      out += '%';
      out += hex[c >> 4];
      out += hex[c & 15];
    }
  }
  return out;
}

std::string fit(const std::string& text, int width) {
  if (width <= 0) return "";
  int w = ftxui::string_width(text);
  if (w <= width) return text + std::string(width - w, ' ');
  // Truncate by glyphs until it fits, leaving room for an ellipsis.
  auto glyphs = ftxui::Utf8ToGlyphs(text);
  std::string out;
  int used = 0;
  for (const auto& g : glyphs) {
    int gw = ftxui::string_width(g);
    if (used + gw > width - 1) break;
    out += g;
    used += gw;
  }
  out += "~";
  ++used;
  return out + std::string(width - used, ' ');
}

}  // namespace reifs
