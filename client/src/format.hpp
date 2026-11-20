// Small formatting helpers shared by the views.
#pragma once

#include <cstdint>
#include <string>

namespace reifs {

std::string human_size(std::int64_t bytes);
// Local time "YYYY-MM-DD HH:MM" for a Unix timestamp (seconds); "" for 0.
std::string format_time(std::int64_t seconds);
// Percent-encode a query parameter value.
std::string url_encode(const std::string& value);
// Pad or truncate to a display width (UTF-8 aware, wide characters count 2).
std::string fit(const std::string& text, int width);

}  // namespace reifs
