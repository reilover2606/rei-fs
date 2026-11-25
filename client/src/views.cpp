// The client's tabs: Overview, Tree, Search, Decisions, Wanted, Jobs, Nodes,
// Marked. Each view keeps its own data and talks to the server through the
// shared Context.
#include <algorithm>
#include <cstdio>
#include <ctime>
#include <map>
#include <set>

#include <ftxui/component/component.hpp>
#include <ftxui/component/component_options.hpp>
#include <ftxui/component/event.hpp>
#include <ftxui/dom/elements.hpp>
#include <ftxui/screen/string.hpp>

#include "app.hpp"
#include "format.hpp"

using namespace ftxui;

namespace reifs {

namespace {

constexpr int kDetailWidth = 56;

bool key(const Event& e, const char* c) { return e.is_character() && e.character() == c; }

std::string strip_offline(const std::string& name) {
  auto pos = name.find(" (offline)");
  return pos == std::string::npos ? name : name.substr(0, pos);
}

constexpr int kLabelWidth = 16;

// Hard-wrap text without spaces (paths) into lines of at most `width` columns.
Element hard_wrap(const std::string& s, int width) {
  Elements lines;
  std::string line;
  int used = 0;
  for (const auto& g : Utf8ToGlyphs(s)) {
    int w = string_width(g);
    if (used + w > width) {
      lines.push_back(text(line));
      line.clear();
      used = 0;
    }
    line += g;
    used += w;
  }
  lines.push_back(text(line));
  return vbox(lines);
}

Element label_value(const std::string& label, Element value) {
  return hbox({text(fit(label, kLabelWidth)) | dim, value | flex});
}

Element label_path(const std::string& label, const std::string& path) {
  return label_value(label, hard_wrap(path, kDetailWidth - kLabelWidth - 1));
}

Element label_value(const std::string& label, const std::string& value) {
  return label_value(label, paragraph(value));
}

Element copies_badge(int good, int goal) {
  auto t = text(std::to_string(good) + " / " + std::to_string(goal));
  if (good >= goal) return t | color(Color::Green);
  if (good >= 1) return t | color(Color::Yellow);
  return t | color(Color::Red);
}

std::string join(const json& arr, const std::string& sep = ", ") {
  std::string out;
  if (!arr.is_array()) return out;
  for (const auto& v : arr) {
    if (!out.empty()) out += sep;
    out += v.is_string() ? v.get<std::string>() : v.dump();
  }
  return out;
}

// Short label for a file entry in a list.
std::string file_label(const json& f, int name_width) {
  std::string flags;
  for (const auto& fl : f.value("flags", json::array())) flags += " " + fl.get<std::string>();
  if (!f.value("issues", json::array()).empty()) flags += " !";
  return fit(f.value("name", f.value("path", "")), name_width) + " " +
         fit(human_size(f.value("size", std::int64_t{0})), 10) + " " +
         fit(std::to_string(f.value("good_copies", 0)) + "/" + std::to_string(f.value("goal", 2)) +
                 flags, 16);
}

// Detail panel for /api/file output.
Element file_details(const json& f) {
  if (f.is_null() || f.empty()) return text("");
  Elements rows = {
      hard_wrap(f.value("path", ""), kDetailWidth - 1) | bold,
      separator(),
      label_value("size", human_size(f.value("size", std::int64_t{0}))),
      label_value("modified", format_time(f.value("mtime", 0))),
      label_value("good copies", copies_badge(f.value("good_copies", 0), f.value("goal", 2))),
  };
  if (!f.value("tags", json::array()).empty())
    rows.push_back(label_value("tags", join(f["tags"])));
  if (f.value("tolerant", false))
    rows.push_back(label_value("versions", text("several versions are fine (no conflicts asked)")));
  if (!f.value("issues", json::array()).empty())
    rows.push_back(label_value("non-portable", paragraph(join(f["issues"])) | color(Color::Yellow)));
  if (!f.value("flags", json::array()).empty())
    rows.push_back(label_value("attention", paragraph(join(f["flags"])) | color(Color::Yellow)));
  rows.push_back(separator());
  rows.push_back(text("copies") | bold);
  for (const auto& c : f.value("copies", json::array())) {
    std::string state = c.value("state", "");
    Color col = Color::Default;
    if (state == "present") {
      state = c.value("current", false) ? "current" : "stale";
      col = c.value("current", false) ? Color::Green : Color::Yellow;
    } else {
      col = Color::Red;
    }
    if (c.value("marked_delete", false)) state += ", marked";
    rows.push_back(hbox({
        text(fit(c.value("node", ""), 14)),
        text(fit(c.value("online", false) ? "online" : "offline", 8)) | dim,
        text(fit(state, 16)) | color(col),
    }));
    rows.push_back(hbox({text("  "), text(format_time(c.value("mtime", 0)) + "  " +
                                         human_size(c.value("size", std::int64_t{0})) + "  " +
                                         c.value("hash", "")) | dim}));
  }
  return vbox(rows);
}

Element list_panel(const std::string& heading, Component menu, size_t count,
                   const std::string& empty_text) {
  Element body = count == 0 ? text(empty_text) | dim | center
                            : menu->Render() | vscroll_indicator | yframe;  // vertical only: clip long rows on the right
  return vbox({text(heading) | bold, separator(), body | flex}) | flex;
}

Element with_detail(Element list, Element detail) {
  return hbox({list, separator(),
               vbox({detail}) | yframe | size(WIDTH, EQUAL, kDetailWidth)});
}

// Ask for a tag and add/remove it on paths.
void tag_paths(Context& ctx, std::vector<std::string> paths, bool recursive, bool add,
               std::function<void()> done) {
  if (paths.empty()) return;
  std::string title = std::string(add ? "Add a tag to " : "Remove a tag from ") +
                      (paths.size() == 1 ? paths[0] : std::to_string(paths.size()) + " items");
  ctx.dialogs->ask_text(title, "", [&ctx, paths, recursive, add, done](const std::string& tag) {
    if (tag.empty()) return;
    ctx.send("/api/tags",
             {{"action", add ? "add" : "remove"}, {"tag", tag}, {"paths", paths},
              {"recursive", recursive}},
             [&ctx, add, done](json out) {
               ctx.notify(std::string(add ? "tagged " : "untagged ") +
                          std::to_string(out.value("changed", 0)) + " file(s)");
               done();
             });
  });
}

// Choose nodes and create a copy or move job.
void transfer_paths(Context& ctx, std::vector<std::string> paths, const std::string& tag,
                    bool move) {
  if (paths.empty() && tag.empty()) return;
  // The nodes, and how many of the selected files each one holds, decide what is offered.
  ctx.run(
      false,
      [paths, tag](Api& a) {
        json body = {{"paths", paths}};
        if (!tag.empty()) body["tag"] = tag;
        return json{{"nodes", a.get("/api/nodes")["nodes"]}, {"holders", a.post("/api/holders", body)}};
      },
      [&ctx, paths, tag, move](json out) {
    int total = out["holders"].value("total", 0);
    std::map<std::string, int> have;
    for (const auto& h : out["holders"]["nodes"]) have[h.value("name", "")] = h.value("have", 0);
    struct Choice {
      std::string name, label;
      int have;
    };
    std::vector<Choice> all;
    for (const auto& n : out["nodes"]) {
      std::string name = n.value("name", "");
      std::string label = name;
      if (total > 0) label += "  (" + std::to_string(have[name]) + " of " + std::to_string(total) + ")";
      if (!n.value("online", false)) label += " (offline)";
      all.push_back({name, label, have[name]});
    }
    if (all.empty()) return;
    auto create = [&ctx, paths, tag, move](const std::string& from, const std::string& to) {
      json body = {{"kind", move ? "move" : "copy"}, {"paths", paths}, {"to", to}};
      if (!tag.empty()) body["tag"] = tag;
      if (!from.empty()) body["from"] = from;
      ctx.send("/api/jobs", body, [&ctx](json out) {
        if (out["job_id"].is_null()) {
          ctx.notify("nothing to do (already there, or not on the source)");
        } else {
          std::string msg = "job #" + std::to_string(out.value("job_id", 0)) + " created with " +
                            std::to_string(out.value("items", 0)) +
                            " item(s); it runs in the background";
          // What was left out and why (files already on the destination are fine).
          std::string left;
          json excluded = out.value("excluded", json::object());
          for (auto& [reason, n] : excluded.items()) {
            if (reason.rfind("already on", 0) == 0) continue;
            left += (left.empty() ? "" : "; ") + n.dump() + " " + reason;
          }
          if (left.empty()) {
            ctx.notify(msg);
          } else {
            ctx.error(msg + ". NOT included: " + left + " (see the job)");
          }
        }
      });
    };
    std::string what = !tag.empty() ? "files tagged '" + tag + "'"
                       : paths.size() == 1 ? paths[0]
                                           : std::to_string(paths.size()) + " items";
    // `skip`: a node left out of the list; `need_files`: only nodes holding something.
    auto pick = [&ctx, all](const std::string& title, const std::string& skip, bool need_files,
                            bool not_full, int total, std::function<void(const std::string&)> done) {
      std::vector<std::string> labels, names;
      for (const auto& c : all) {
        if (c.name == skip) continue;
        if (need_files && c.have == 0) continue;
        if (not_full && total > 0 && c.have >= total) continue;   // it has them all already
        labels.push_back(c.label);
        names.push_back(c.name);
      }
      if (labels.empty()) {
        ctx.notify(need_files ? "no node holds any of these files"
                              : "no other node to put them on");
        return;
      }
      ctx.dialogs->ask_choice(title, labels, [names, done](int i) { done(names[i]); });
    };
    if (!move) {
      pick("Copy " + what + " to which node?  (files held / selected)", "", false, true, total,
           [create](const std::string& to) { create("", to); });
      return;
    }
    pick("Move " + what + " FROM which node? (its copies get marked for deletion)", "", true, false,
         total, [&ctx, create, what, pick, total](const std::string& from) {
           pick("Move " + what + " from " + from + " TO which node?", from, false, false, total,
                [create, from](const std::string& to) { create(from, to); });
         });
  });
}

// Download files (or everything below a folder) to this machine: into the "fetch" folder at the
// root of the server's own node, where nothing is indexed. The files may be fetched from other
// servers, through intermediate ones.
void fetch_paths(Context& ctx, std::vector<std::string> paths, const std::string& tag) {
  if (paths.empty() && tag.empty()) return;
  std::string what = !tag.empty() ? "the files tagged '" + tag + "'"
                     : paths.size() == 1 ? paths[0] : std::to_string(paths.size()) + " items";
  ctx.dialogs->confirm("Fetch " + what + " (all its files) to this machine?\n"
                       "It is put in the fetch folder at the root of this server's node, "
                       "outside the virtual tree.",
                       [&ctx, paths, tag] {
    json body = {{"kind", "fetch"}, {"paths", paths}};
    if (!tag.empty()) body["tag"] = tag;
    ctx.send("/api/jobs", body, [&ctx](json out) {
      if (!out["job_id"].is_number()) {
        ctx.notify("nothing to fetch");
        return;
      }
      ctx.notify("job #" + std::to_string(out["job_id"].get<long>()) + ": fetching " +
                 std::to_string(out.value("items", 0)) + " file(s) to " +
                 out.value("dest", "the fetch folder"));
    });
  });
}

// --- Scheduled syncs ----------------------------------------------------------------

const std::vector<std::pair<std::string, std::string>>& interval_choices() {
  static const std::vector<std::pair<std::string, std::string>> v = {
      {"every 15 minutes", "15m"}, {"every hour", "1h"}, {"every 6 hours", "6h"},
      {"every day", "1d"},         {"every week", "1w"}};
  return v;
}

// Define a scheduled sync of `path` (asks the nodes and the interval).
void schedule_dialog(Context& ctx, std::string path, std::function<void()> done) {
  ctx.get("/api/nodes", [&ctx, path, done](json out) {
    std::vector<std::string> names, labels;
    for (const auto& n : out["nodes"]) {
      names.push_back(n.value("name", ""));
      labels.push_back(n.value("name", "") +
                       (n.value("online", false) ? "" : " (offline)") +
                       (n.value("owner", "") == "self" ? "" : "  [server " + n.value("owner", "") + "]"));
    }
    if (names.size() < 2) {
      ctx.error("a sync needs two nodes");
      return;
    }
    ctx.dialogs->ask_choice(
        "Keep " + path + " synced FROM which node (the source)?", labels,
        [&ctx, path, done, names, labels](int s) {
          ctx.dialogs->ask_choice(
              "...TO which node (the backup)? Its server runs the sync.", labels,
              [&ctx, path, done, names, s](int d) {
                std::vector<std::string> opts;
                for (const auto& [label, _] : interval_choices()) opts.push_back(label);
                ctx.dialogs->ask_choice(
                    "How often? (If a node is asleep when it is due, it runs when it is back.)",
                    opts, [&ctx, path, done, names, s, d](int i) {
                      json body = {{"path", path}, {"from", names[s]}, {"to", names[d]},
                                   {"every", interval_choices()[i].second}};
                      ctx.send("/api/schedules", body, [&ctx, done](json) {
                        ctx.notify("scheduled; the first run starts now");
                        if (done) done();
                      });
                    });
              });
        });
  });
}

// Move items inside the virtual tree: the files are renamed on every node that
// holds them. When the destination already has something of the same name the
// user chooses between merging (the default) and keeping both.
void virtual_move(Context& ctx, std::vector<std::string> paths, const std::string& dest,
                  const std::string& name, std::function<void()> done) {
  if (paths.empty()) return;
  json probe = {{"paths", paths}, {"to", dest}, {"dry_run", true}};
  if (!name.empty()) probe["name"] = name;
  ctx.send("/api/vmove", probe, [&ctx, paths, dest, name, done](json plan) {
    if (plan.value("files", 0) == 0 && plan.value("dirs", 0) == 0) {
      ctx.notify("nothing to move (it is already there)");
      return;
    }
    auto go = [&ctx, paths, dest, name, done](const std::string& mode) {
      json body = {{"paths", paths}, {"to", dest}, {"on_conflict", mode}};
      if (!name.empty()) body["name"] = name;
      ctx.send("/api/vmove", body, [&ctx, done](json out) {
        std::string msg = out.value("files", 0) == 0
                              ? "moved"
                              : std::to_string(out.value("files", 0)) + " file(s) moved";
        // job_id is null when only other servers have copies to rename (value() would throw).
        if (out["job_id"].is_number())
          msg += "; renaming on the nodes (job #" + std::to_string(out["job_id"].get<long>()) + ")";
        else if (out["remote_jobs"].is_number() && out["remote_jobs"].get<int>() > 0)
          msg += "; the other servers are renaming on their nodes";
        if (out.value("merged", 0) > 0)
          msg += ", " + std::to_string(out.value("merged", 0)) + " merged";
        if (out.value("marked", 0) > 0)
          msg += ", " + std::to_string(out.value("marked", 0)) +
                 " duplicate cop(ies) marked for deletion";
        if (!out["skipped"].empty())
          msg += ", " + std::to_string(out["skipped"].size()) + " skipped";
        if (!out["errors"].empty()) {
          msg += "; could not reach: " + out["errors"][0].get<std::string>();
          ctx.error(msg);
        } else {
          ctx.notify(msg);
        }
        if (done) done();
      });
    };
    if (plan["conflicts"].empty()) {
      go("merge");
      return;
    }
    std::string names;
    int shown = 0;
    for (const auto& c : plan["conflicts"]) {
      if (shown++ == 3) {
        names += ", ...";
        break;
      }
      std::string full = c.get<std::string>();
      names += (names.empty() ? "" : ", ") + full.substr(full.rfind('/') + 1);
    }
    ctx.dialogs->ask_choice(
        names + " already exists in " + dest + ".\nMerge: folders are combined; identical files "
                "become one (the duplicate is marked for deletion); different content becomes "
                "two versions of one file.\nKeep both: the moved item gets a free name.",
        {"Merge (default)", "Keep both", "Cancel"}, [go](int i) {
          if (i == 0) go("merge");
          if (i == 1) go("preserve");
        });
  });
}

// Keep the selection on the same item (by key) across refreshes.
template <typename KeyFn>
int reselect(const json& items, const std::string& previous, KeyFn keyfn, int fallback) {
  for (size_t i = 0; i < items.size(); ++i)
    if (keyfn(items[i]) == previous) return static_cast<int>(i);
  return std::clamp(fallback, 0, std::max(0, static_cast<int>(items.size()) - 1));
}

// --- Overview ---------------------------------------------------------------------

// Seconds since the epoch of "2026-11-20T21:34:42.786160+00:00" (UTC); 0 if unreadable.
std::int64_t parse_utc(const std::string& iso) {
  int y, mo, d, h, mi, sec;
  if (std::sscanf(iso.c_str(), "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &sec) != 6) return 0;
  std::tm tm{};
  tm.tm_year = y - 1900;
  tm.tm_mon = mo - 1;
  tm.tm_mday = d;
  tm.tm_hour = h;
  tm.tm_min = mi;
  tm.tm_sec = sec;
#ifdef _WIN32
  return static_cast<std::int64_t>(_mkgmtime(&tm));
#else
  return static_cast<std::int64_t>(timegm(&tm));
#endif
}

std::string duration_text(double seconds) {
  auto total = static_cast<long>(seconds < 0 ? 0 : seconds);
  char buf[48];
  if (total < 60)
    std::snprintf(buf, sizeof buf, "%ld s", total);
  else if (total < 3600)
    std::snprintf(buf, sizeof buf, "%ld min %02ld s", total / 60, total % 60);
  else
    std::snprintf(buf, sizeof buf, "%ld h %02ld min", total / 3600, (total % 3600) / 60);
  return buf;
}


class OverviewView : public View {
 public:
  explicit OverviewView(Context& ctx) : ctx_(ctx) {
    // The Overview is taller than many terminals: it scrolls (arrows, PageUp/PageDown, Home/End).
    root_ = Renderer([this] {
      return render() | focusPositionRelative(0.f, scroll_) | vscroll_indicator | yframe;
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Overview"; }
  Component component() override { return root_; }
  void refresh() override {
    ctx_.get("/api/replication", [this](json out) { replication_ = std::move(out); },
             "overview-replication");
    ctx_.get("/api/network", [this](json out) { network_ = std::move(out); }, "overview-network");
  }
  void on_poll() override {
    if (++polls_ % 15 == 0) refresh();  // the backup plan changes rarely (~30 s)
  }
  std::string help() const override {
    return "↑/↓/PgUp/PgDn: scroll  s: scan all  w: new files only  j: run jobs  p: plan backups  "
           "g: copy goal  a: auto-approve  x: stop";
  }

 private:
  bool on_event(const Event& e) {
    if (e == Event::ArrowDown) return scroll_ = std::min(1.f, scroll_ + 0.1f), true;
    if (e == Event::ArrowUp) return scroll_ = std::max(0.f, scroll_ - 0.1f), true;
    if (e == Event::PageDown) return scroll_ = std::min(1.f, scroll_ + 0.5f), true;
    if (e == Event::PageUp) return scroll_ = std::max(0.f, scroll_ - 0.5f), true;
    if (e == Event::Home) return scroll_ = 0.f, true;
    if (e == Event::End) return scroll_ = 1.f, true;
    if (key(e, "s")) {
      ctx_.send("/api/scan", {}, [this](json) { ctx_.notify("scan queued"); });
      return true;
    }
    if (key(e, "w")) {
      ctx_.send("/api/scan", {{"new_only", true}},
                [this](json) { ctx_.notify("scan for new files queued"); });
      return true;
    }
    if (key(e, "j")) {
      ctx_.send("/api/jobs/run", {}, [this](json) { ctx_.notify("job run queued"); });
      return true;
    }
    if (key(e, "p")) {
      ctx_.send("/api/replication/plan", {},
                [this](json) { ctx_.notify("backup planning queued"); });
      return true;
    }
    if (key(e, "g")) {
      std::string current = std::to_string(replication_.value("copies", 2));
      ctx_.dialogs->ask_text("Good copies every file should have (tag-specific goals: "
                             "[replication] tag_copies in config.toml)",
                             current, [this](const std::string& value) {
                               int n = 0;
                               try {
                                 n = std::stoi(value);
                               } catch (...) {
                                 ctx_.error("not a number: " + value);
                                 return;
                               }
                               ctx_.send("/api/replication", {{"copies", n}}, [this, n](json) {
                                 ctx_.notify("copy goal set to " + std::to_string(n));
                                 refresh();
                               });
                             });
      return true;
    }
    if (key(e, "a")) {
      bool on = replication_.value("auto_replicate", false);
      std::string q = on ? "Turn OFF automatic approval of backup copies?"
                         : "Turn ON automatic approval? Second copies of under-replicated files "
                           "will then be made without asking (restores, renames and overwrites "
                           "still wait for you).";
      ctx_.dialogs->confirm(q, [this, on] {
        ctx_.send("/api/replication", {{"auto_replicate", !on}}, [this, on](json) {
          ctx_.notify(std::string("automatic approval ") + (on ? "off" : "on"));
          refresh();
        });
      });
      return true;
    }
    if (key(e, "x")) {
      ctx_.dialogs->confirm("Stop the current scan or job run? (progress is kept)", [this] {
        ctx_.send("/api/stop", {}, [this](json) { ctx_.notify("stop requested"); });
      });
      return true;
    }
    return false;
  }

  Element render() {
    json s = ctx_.status();
    if (s.empty()) {
      auto waited = std::chrono::duration_cast<std::chrono::seconds>(
                        std::chrono::steady_clock::now() - ctx_.connected_at).count();
      Elements lines = {text("waiting for the server's status... (" + std::to_string(waited) +
                             " s)") | center};
      std::string err = ctx_.poll_error();
      if (!err.empty()) lines.push_back(paragraph("last error: " + err) | color(Color::Red) | center);
      if (waited > 10)
        lines.push_back(paragraph("The server may be busy (first scan of many files). "
                                  "Other tabs work meanwhile.") | dim | center);
      return vbox(lines) | center;
    }
    auto act = s.value("activity", json::object());
    auto num = [&](const char* k) { return std::to_string(s.value(k, 0)); };
    auto line = [&](const std::string& label, const std::string& value, Color c = Color::Default,
                    bool highlight = false) {
      auto v = text(value);
      if (highlight) v = v | color(c) | bold;
      return hbox({text(fit(label, 26)), v});
    };
    int under = s.value("under_replicated", 0);
    Elements left = {
        text("Index") | bold,
        separator(),
        line("files", num("files") + "  (" + human_size(s.value("bytes", std::int64_t{0})) + ")"),
        line("with at least 2 copies", num("satisfied"), Color::Green, true),
        line("under-replicated", num("under_replicated"), Color::Red, under > 0),
        line("  with a single copy", num("single_copy"), Color::Red, s.value("single_copy", 0) > 0),
        line("divergent versions", num("divergent"), Color::Yellow, s.value("divergent", 0) > 0),
        line("missing copies", num("missing_copies"), Color::Yellow,
             s.value("missing_copies", 0) > 0),
        line("marked for deletion", num("marked_delete"), Color::Cyan,
             s.value("marked_delete", 0) > 0),
        line("non-portable names", num("non_portable"), Color::Yellow,
             s.value("non_portable", 0) > 0),
    };
    Elements right = {
        text("Server") | bold,
        separator(),
        line("name", s["server"].value("name", "")),
        line("nodes online", num("nodes_online") + " / " + num("nodes_total")),
        line("open decisions", num("open_decisions"), Color::Yellow,
             s.value("open_decisions", 0) > 0),
        line("wanted jobs", num("open_wanted"), Color::Cyan, s.value("open_wanted", 0) > 0),
        line("active jobs", num("active_jobs"), Color::Cyan, s.value("active_jobs", 0) > 0),
    };

    // What the server is busy with, and what the database is doing.
    Elements work = {text("Activity and database") | bold, separator()};
    std::string state = act.value("state", "idle");
    if (state != "idle") {
      double elapsed = static_cast<double>(std::time(nullptr) - parse_utc(act.value("since", "")));
      work.push_back(hbox({text(fit("working on", 26)),
                           text(state + " for " + duration_text(elapsed)) | color(Color::Yellow) |
                               bold}));
      if (!act.value("detail", "").empty())
        work.push_back(hbox({text(fit("", 26)), paragraph(act.value("detail", ""))}));
      if (act.value("queued", 0) > 0)
        work.push_back(line("queued after that", std::to_string(act.value("queued", 0))));
    } else {
      work.push_back(line("working on", "nothing (idle)"));
      if (!act.value("last_result", "").empty())
        work.push_back(hbox({text(fit("last result", 26)), paragraph(act.value("last_result", ""))}));
    }
    if (!act.value("last_error", "").empty())
      work.push_back(hbox({text(fit("last error", 26)),
                           paragraph(act.value("last_error", "")) | color(Color::Red)}));
    json dbs = s.value("database", json::object());
    if (!dbs.empty()) {
      std::string lock = dbs.value("lock", "free");
      if (lock == "free") {
        work.push_back(hbox({text(fit("database write lock", 26)), text("free") | color(Color::Green)}));
      } else if (lock == "held") {
        std::string who;
        for (const auto& h : dbs["holders"]) {
          if (!who.empty()) who += ", ";
          char buf[32];
          std::snprintf(buf, sizeof buf, " (%.1f s)", h.value("seconds", 0.0));
          who += h.value("who", "?") + buf;
        }
        work.push_back(hbox({text(fit("database write lock", 26)),
                             paragraph("in use by " + who) | color(Color::Yellow)}));
      } else {
        work.push_back(hbox({text(fit("database write lock", 26)),
                             paragraph("LOCKED by another program (for example a reifs command "
                                       "running in a terminal)") |
                                 color(Color::Red) | bold}));
      }
      if (!dbs["waiting"].empty()) {
        double longest = 0;
        for (const auto& w : dbs["waiting"]) longest = std::max(longest, w.value("seconds", 0.0));
        char buf[96];
        std::snprintf(buf, sizeof buf, "%zu waiting for it (longest %.1f s)", dbs["waiting"].size(),
                      longest);
        work.push_back(hbox({text(fit("writers", 26)),
                             text(buf) | color(longest > 5 ? Color::Red : Color::Yellow)}));
      }
      int requests = dbs.value("requests", 0);
      char rbuf[96];
      if (requests > 0)
        std::snprintf(rbuf, sizeof rbuf, "%d being served (slowest %.1f s)", requests,
                      dbs.value("slowest_request", 0.0));
      else
        std::snprintf(rbuf, sizeof rbuf, "none");
      work.push_back(line("client requests", rbuf));
      if (dbs.value("busy_errors", 0) > 0)
        work.push_back(line("failed: database busy", std::to_string(dbs.value("busy_errors", 0)),
                            Color::Red, true));
      json conns = s.value("connections", json::object());
      if (conns.value("handshake_failed", 0) > 0 || conns.value("dropped", 0) > 0)
        work.push_back(line("connections that failed",
                            std::to_string(conns.value("handshake_failed", 0)) +
                                " TLS handshakes, " + std::to_string(conns.value("dropped", 0)) +
                                " dropped (details in the server's log)",
                            Color::Yellow, true));
      work.push_back(text("Reading never waits for the write lock; only changes do.") | dim);
    }
    std::string goal = std::to_string(replication_.value("copies", 2)) + " copies of every file";
    // Keep the json alive for the loop: .items() on a temporary dangles (C++20).
    json tag_copies = replication_.value("tag_copies", json::object());
    for (auto& [tag, n] : tag_copies.items())
      goal += ", " + n.dump() + " for '" + tag + "'";
    Elements backup = {text("Backups (goal: " + goal + ")") | bold, separator()};
    bool autom = replication_.value("auto_replicate", false);
    backup.push_back(line("automatic approval", autom ? "on" : "off", Color::Green, autom));
    backup.push_back(line("open proposals", std::to_string(replication_.value("open_proposals", 0)),
                          Color::Cyan, replication_.value("open_proposals", 0) > 0));
    json lp = replication_.value("last_plan", json());
    if (lp.is_object()) {
      backup.push_back(line("last plan", lp.value("finished", "").substr(0, 19)));
      backup.push_back(line("  files below goal", std::to_string(lp.value("under_replicated",
                                                                          lp.value("single_copy", 0))) +
                                                     " (" + std::to_string(lp.value("responsible", 0)) +
                                                     " planned by this server)"));
      backup.push_back(line("  copies proposed", std::to_string(lp.value("proposed", 0)) +
                                                     (lp.value("truncated", false)
                                                          ? " (limit reached: more as these are decided)"
                                                          : "")));
      if (lp.value("auto_approved", 0) > 0)
        backup.push_back(line("  approved automatically", std::to_string(lp.value("auto_approved", 0))));
      int nodest = lp.value("no_destination", 0);
      backup.push_back(line("  no possible destination", std::to_string(nodest), Color::Red,
                            nodest > 0));
      json reasons = lp.value("reasons", json::object());  // not a temporary: see above
      for (auto& [reason, n] : reasons.items())
        backup.push_back(hbox({text("      " + n.dump() + "  "), paragraph(reason) | dim}));
    } else {
      backup.push_back(text("not planned yet (p: plan now)") | dim);
    }
    return vbox({hbox({vbox(left) | flex | border, vbox(right) | flex | border}),
                 vbox(work) | border, vbox(backup) | border, network_panel()});
  }

  // --- the whole network: where the copies are, and how well files are covered -------------

  static Color node_color(size_t i) {
    static const Color palette[] = {Color::Cyan,  Color::Green,       Color::Yellow, Color::Magenta,
                                    Color::Blue,  Color::GreenLight,  Color::Red,    Color::CyanLight,
                                    Color::YellowLight, Color::MagentaLight};
    return palette[i % (sizeof palette / sizeof palette[0])];
  }

  // A bar split into coloured segments, one per node, in proportion to `values`.
  static Element stacked_bar(const std::vector<double>& values, int width) {
    double total = 0;
    for (double v : values) total += v;
    Elements segs;
    int used = 0;
    for (size_t i = 0; i < values.size(); ++i) {
      int w = total > 0 ? static_cast<int>(values[i] / total * width + 0.5) : 0;
      if (i + 1 == values.size()) w = std::max(0, width - used);   // the rounding remainder
      if (w <= 0) continue;
      used += w;
      segs.push_back(text(std::string(w, ' ')) | bgcolor(node_color(i)));
    }
    if (segs.empty()) segs.push_back(text(std::string(width, ' ')) | bgcolor(Color::GrayDark));
    return hbox(segs);
  }

  static std::string percent(double part, double whole) {
    char buf[16];
    std::snprintf(buf, sizeof buf, "%5.1f%%", whole > 0 ? part * 100.0 / whole : 0.0);
    return buf;
  }

  Element network_panel() const {
    if (!network_.is_object() || !network_.contains("nodes"))
      return vbox({text("Network") | bold, text("loading...") | dim}) | border;
    int goal = network_.value("goal", 2);
    double files = network_.value("files", 0), satisfied = network_.value("satisfied", 0);
    int under = network_.value("under", 0), single = network_.value("single", 0);
    Elements rows = {hbox({text("Network") | bold,
                           text(network_.value("stale", false) ? "  (figures a few seconds old)" : "") | dim}),
                     separator()};

    // 1. How many files meet their replication goal.
    double frac = files > 0 ? satisfied / files : 1.0;
    Color goal_color = frac >= 0.999 ? Color::Green : frac >= 0.75 ? Color::Yellow : Color::Red;
    rows.push_back(hbox({text("replication goal (" + std::to_string(goal) + " copies)  "),
                         gauge(static_cast<float>(frac)) | color(goal_color) | flex,
                         text("  " + percent(satisfied, files) + " meet it") | bold | color(goal_color)}));
    rows.push_back(text("  " + std::to_string(static_cast<long>(satisfied)) + " of " +
                        std::to_string(static_cast<long>(files)) + " files meet their goal, " +
                        std::to_string(under) + " below it (" + std::to_string(single) +
                        " with a single copy)") | dim);
    // Files by number of good copies.
    const json& hist = network_["hist"];
    double peak = 1;
    for (const auto& h : hist) peak = std::max(peak, h.get<double>());
    static const char* names[] = {"no good copy", "1 copy", "2 copies", "3 copies", "4+ copies"};
    for (size_t i = 0; i < hist.size() && i < 5; ++i) {
      Color c = i == 0 ? Color::Red : (static_cast<int>(i) < goal ? Color::Yellow : Color::Green);
      rows.push_back(hbox({text("  " + fit(names[i], 14)), gauge(static_cast<float>(hist[i].get<double>() / peak)) |
                                                               color(c) | size(WIDTH, EQUAL, 36),
                           text("  " + std::to_string(hist[i].get<long>()) + " files")}));
    }
    rows.push_back(separator());

    // 2. Where the copies are: by number of files and, in proportion, by size.
    std::vector<double> nfiles, nbytes;
    double tfiles = 0, tbytes = 0;
    for (const auto& n : network_["nodes"]) {
      nfiles.push_back(n.value("files", 0));
      nbytes.push_back(static_cast<double>(n.value("bytes", std::int64_t{0})));
      tfiles += nfiles.back();
      tbytes += nbytes.back();
    }
    rows.push_back(text("where the copies are (a file on two nodes counts on both)") | dim);
    rows.push_back(hbox({text(fit("  by files", 11)), stacked_bar(nfiles, 60)}));
    rows.push_back(hbox({text(fit("  by size", 11)), stacked_bar(nbytes, 60)}));
    size_t i = 0;
    for (const auto& n : network_["nodes"]) {
      Color c = node_color(i);
      double f = nfiles[i], b = nbytes[i];
      rows.push_back(hbox({text("  ■ ") | color(c), text(fit(n.value("name", ""), 16)),
                           text(fit(std::to_string(static_cast<long>(f)) + " files", 13)),
                           gauge(static_cast<float>(tfiles > 0 ? f / tfiles : 0)) | color(c) |
                               size(WIDTH, EQUAL, 14),
                           text(" " + percent(f, tfiles) + "  "), text(fit(human_size(static_cast<std::int64_t>(b)), 10)),
                           gauge(static_cast<float>(tbytes > 0 ? b / tbytes : 0)) | color(c) |
                               size(WIDTH, EQUAL, 14),
                           text(" " + percent(b, tbytes)),
                           text(n.value("online", false) ? "" : "  offline") | dim}));
      ++i;
    }
    return vbox(rows) | border;
  }

  Context& ctx_;
  float scroll_ = 0.f;   // 0 = top, 1 = bottom
  json network_ = json::object();
  json replication_ = json::object();
  int polls_ = 0;
  Component root_;
};

// --- Tree ---------------------------------------------------------------------------

const char* priority_name(int p) {
  static const char* names[] = {"synchronous", "frequent", "infrequent", "archival"};
  return names[std::clamp(p, 0, 3)];
}


class TreeView : public View {
 public:
  explicit TreeView(Context& ctx) : ctx_(ctx) {
    MenuOption opt = MenuOption::Vertical();
    // Like the default look, but entries with a copy on this server's own node are blue.
    opt.entries_option.transform = [this](const EntryState& st) {
      Element e = text((st.active ? "> " : "  ") + st.label);
      bool here = st.index >= 0 && st.index < static_cast<int>(entries_.size()) &&
                  has_here(entries_[st.index]);
      if (here) e = e | color(Color::BlueLight);
      if (st.focused) e = e | inverted;
      if (st.active) e = e | bold;
      if (!st.focused && !st.active && !here) e = e | dim;
      return e;
    };
    opt.on_change = [this] { load_detail(); };
    opt.on_enter = [this] { enter(); };
    menu_ = Menu(&labels_, &selected_, opt);
    root_ = Renderer(menu_, [this] {
      std::string heading = path_;
      if (dir_priority_ != 2) heading += std::string("  [") + priority_name(dir_priority_) + "]";
      if (more_files_ > 0)
        heading += "   (" + std::to_string(more_files_) + " more files not listed; use Search)";
      if (!filter_.empty())
        heading += "   [showing names with '" + filter_ + "': " + std::to_string(entries_.size()) +
                   " of " + std::to_string(all_entries_.size()) + "; Esc clears]";
      if (!inside_.empty()) heading += "   [inside " + inside_ + ": read-only, from the archive's list]";
      if (!picked_.empty())
        heading += "   [" + std::to_string(picked_.size()) + " picked up: p puts them here]";
      return with_detail(list_panel(heading, menu_, labels_.size(),
                                    loading_ ? "loading..." : "(empty)"),
                         detail_);
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Tree"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "Enter/→: open  ←: up  t/T: tag  c: copy  m: move to a node  x: pick up  p: put here  f: fetch  S: schedule sync  "
           "v: move to...  n: rename  d: new dir  D: remove empty dir  u: update priority  "
           "z: pack/unpack  Z: drop archive  V: several versions ok  /: filter this folder  r: refresh";
  }
  void on_poll() override {
    // After a virtual move the renames run in the background: follow them.
    if (follow_ > 0) {
      --follow_;
      refresh();
    }
  }

  void refresh() override {
    refresh_selecting(want_.empty() ? current_name() : want_);
    want_.clear();
  }
  void navigate(const std::string& dir, const std::string& select) override {
    path_ = dir.empty() ? "/" : dir;
    want_ = select;
    selected_ = 0;
    filter_.clear();
    all_entries_ = json::array();
    entries_ = json::array();
    labels_.clear();
    detail_ = text("");
    loading_ = true;
  }

 private:
  // Load the listing of path_ and, when it arrives, select `name` in it.
  void refresh_selecting(const std::string& name) {
    std::string requested = path_;
    ctx_.run(
        false, [requested](Api& a) { return a.get("/api/tree?path=" + url_encode(requested)); },
        [this, requested, name](json out) {
          if (requested != path_) return;   // the user has gone elsewhere meanwhile
          loading_ = false;
          all_entries_ = out["entries"];
          entries_ = filtered(all_entries_);
          dir_priority_ = out.value("priority", 2);
          this_node_ = out.value("this_node", "");
          inside_ = out.value("inside_archive", "");
          more_files_ = out.value("more_files", 0);
          rebuild_labels();
          selected_ = reselect(entries_, name,
                               [](const json& e) { return e.value("name", ""); }, selected_);
          load_detail();
        },
        "tree-list",
        [this, requested] {
          if (requested != path_) return;
          loading_ = false;
          if (path_ != "/") {   // the directory is gone: go back to the top
            path_ = "/";
            refresh_selecting("");
          }
        });
  }

  // The entries whose name contains the typed filter (case-insensitive); all of them if none.
  json filtered(const json& all) const {
    if (filter_.empty()) return all;
    json out = json::array();
    std::string needle = lower(filter_);
    for (const auto& e : all)
      if (lower(e.value("name", "")).find(needle) != std::string::npos) out.push_back(e);
    return out;
  }
  static std::string lower(std::string s) {
    for (auto& c : s)
      if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    return s;
  }
  void set_filter(const std::string& f) {
    filter_ = f;
    std::string keep = current_name();
    entries_ = filtered(all_entries_);
    rebuild_labels();
    selected_ = reselect(entries_, keep, [](const json& e) { return e.value("name", ""); }, 0);
    load_detail();
  }

  // Does this entry (a file) or something below it (a folder) have a copy on this server's node?
  static bool has_here(const json& e) {
    auto h = e.find("here");
    if (h == e.end()) return false;
    return h->is_boolean() ? h->get<bool>() : h->get<int>() > 0;
  }

  // "»" marks the items picked up for a move.
  void rebuild_labels() {
    labels_.clear();
    for (const auto& e : entries_) {
      bool picked = picked_.count(child_path(e.value("name", ""))) > 0;
      std::string mark = picked ? "» " : "  ";
      if (e.value("in_archive", false) && !e.value("dir", false))
        labels_.push_back(mark + fit(e.value("name", ""), 40) + " " +
                          fit(human_size(e.value("size", std::int64_t{0})), 10) + " in archive");
      else if (e.value("packed", false))
        labels_.push_back(mark + fit("▣ " + e.value("name", "") + "/", 34) + " " +
                          fit(std::to_string(e.value("files", 0)) + " files", 11) + " [packed]");
      else if (e.value("dir", false))
        labels_.push_back(mark + fit("▸ " + e.value("name", "") + "/", 34) + " " +
                          fit(std::to_string(e.value("files", 0)) + " files", 11) +
                          (e.value("priority", 2) != 2
                               ? std::string(" [") + priority_name(e.value("priority", 2)) + "]"
                               : ""));
      else
        labels_.push_back(mark + file_label(e, 40));
    }
  }

  std::string current_name() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(entries_.size())) return "";
    return entries_[selected_].value("name", "");
  }
  std::string child_path(const std::string& name) const {
    return path_ == "/" ? "/" + name : path_ + "/" + name;
  }
  const json* current() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(entries_.size())) return nullptr;
    return &entries_[selected_];
  }

  // Does the folder exist on this server's node? (blue = yes, at least one file)
  Element here_line(const json& dir) const {
    int here = dir.value("here", 0), total = dir.value("files", 0);
    std::string node = this_node_.empty() ? "this server's node" : "this server's node (" + this_node_ + ")";
    if (here > 0)
      return text("on " + node + ": " + std::to_string(here) + " of " + std::to_string(total) +
                  " file(s)") | color(Color::BlueLight);
    return text("not on " + node + (total > 0 ? ": no file below it has a copy there" : "")) | dim;
  }

  void load_detail() {
    const json* e = current();
    if (!e) {
      detail_ = text("");
      return;
    }
    if (e->value("in_archive", false)) {
      std::string p = child_path(e->value("name", ""));
      bool d = e->value("dir", false);
      detail_ = vbox({hard_wrap(p + (d ? "/" : ""), kDetailWidth - 1) | bold, separator(),
                      text(d ? std::to_string(e->value("files", 0)) + " file(s) below"
                             : "size: " + human_size(e->value("size", std::int64_t{0}))),
                      text("inside the packed directory " + inside_),
                      text(""), text("read-only: it is listed from the archive's contents") | dim,
                      text("go up to the packed directory: z unpacks it") | dim});
      return;
    }
    if (e->value("packed", false)) {
      std::string dir = child_path(e->value("name", ""));
      std::string archive = e->value("archive", dir + ".sqsh");
      Element head = vbox({hard_wrap(dir + "/", kDetailWidth - 1) | bold,
                           text("packed: " + std::to_string(e->value("files", 0)) +
                                " files, " + human_size(e->value("bytes", std::int64_t{0})) +
                                " before packing"),
                           text("Enter: look inside (read-only)") | dim,
                           text("z: unpack it  Z: drop the archive") | dim,
                           text("c/m/f: copy, move or fetch the archive") | dim,
                           text("search with 'deep search' to look inside") | dim,
                           separator()});
      detail_ = vbox({head, text("the archive " + archive + ":") | dim});
      ctx_.run(
          false, [archive](Api& a) { return a.get("/api/file?path=" + url_encode(archive)); },
          [this, dir, head](json f) {
            const json* cur = current();
            if (!cur || child_path(cur->value("name", "")) != dir) return;
            detail_ = vbox({head, file_details(f)});
          },
          "tree-detail");
      return;
    }
    if (e->value("dir", false)) {
      detail_ = vbox({hard_wrap(child_path(e->value("name", "")) + "/", kDetailWidth - 1) | bold,
                      separator(),
                      text(std::to_string(e->value("files", 0)) + " file(s) below"),
                      text(std::string("update priority: ") + priority_name(e->value("priority", 2)) +
                           (e->value("set_here", false) ? "" : " (inherited)")),
                      here_line(*e),
                      text(std::string("versions: ") + (e->value("tolerant", false)
                               ? "several are fine (no conflicts asked)" : "conflicts are decided")),
                      text(""), text("t: tag every file below  c/m: copy/move it all") | dim,
                      text("V: several versions are fine here (no conflict decisions)") | dim,
                      text("x: pick up to move it in the tree  n: rename") | dim,
                      text("u: update priority (how often it is scanned)") | dim,
                      text("f: fetch it to this machine (the fetch folder)") | dim,
                      text("z: pack it into one squashfs archive") | dim,
                      text("blue: a copy is on this server's own node") | dim});
      return;
    }
    // Only the detail of the item the user stops on is fetched (same key).
    std::string path = child_path(e->value("name", ""));
    ctx_.run(
        false, [path](Api& a) { return a.get("/api/file?path=" + url_encode(path)); },
        [this, path](json f) {
          const json* cur = current();
          if (!cur || cur->value("dir", false) ||
              child_path(cur->value("name", "")) != path)
            return;   // selection moved on
          detail_ = file_details(f);
        },
        "tree-detail");
  }

  void enter() {
    const json* e = current();
    if (!e || !e->value("dir", false)) return;
    path_ = child_path(e->value("name", ""));
    filter_.clear();
    all_entries_ = json::array();
    selected_ = 0;
    entries_ = json::array();
    labels_.clear();
    detail_ = text("");
    loading_ = true;
    refresh_selecting("");
  }

  void up() {
    if (path_ == "/") return;
    auto pos = path_.rfind('/');
    std::string child = path_.substr(pos + 1);
    path_ = pos == 0 ? "/" : path_.substr(0, pos);
    filter_.clear();
    all_entries_ = json::array();
    entries_ = json::array();
    labels_.clear();
    detail_ = text("");
    loading_ = true;
    refresh_selecting(child);
  }

  bool on_event(const Event& e) {
    if (e == Event::Backspace || e == Event::ArrowLeft) {
      up();
      return true;
    }
    if (e == Event::ArrowRight) {
      enter();
      return true;
    }
    if (key(e, "r")) {
      refresh();
      return true;
    }
    if (!inside_.empty()) {   // what is listed from an archive cannot be acted on
      for (const char* k : {"t", "T", "c", "m", "f", "x", "n", "v", "S", "D", "d", "u", "p", "z", "Z", "V"})
        if (key(e, k)) {
          ctx_.notify("inside the packed directory " + inside_ +
                      ": read-only (go up to it; z there unpacks it)");
          return true;
        }
    }
    if (key(e, "p") && !picked_.empty()) {   // put the picked-up items here
      move_items({picked_.begin(), picked_.end()}, path_, "");
      return true;
    }
    if (e == Event::Escape && !filter_.empty()) {
      set_filter("");
      return true;
    }
    if (key(e, "/")) {   // find among the entries of this folder
      ctx_.dialogs->ask_text("Show only the entries of " + path_ + " whose name contains:",
                             filter_, [this](const std::string& f) { set_filter(f); });
      return true;
    }
    if (e == Event::Escape && !picked_.empty()) {
      picked_.clear();
      rebuild_labels();
      return true;
    }
    if (key(e, "d")) {   // new (empty) directory here
      ctx_.dialogs->ask_text("New directory in " + path_ + " (a/b/c makes several):", "",
                             [this](const std::string& name) {
        if (name.empty()) return;
        std::string full = path_ == "/" ? "/" + name : path_ + "/" + name;
        ctx_.send("/api/mkdir", {{"path", full}}, [this, full](json) {
          ctx_.notify("created " + full);
          want_ = full.substr(path_ == "/" ? 1 : path_.size() + 1);
          want_ = want_.substr(0, want_.find('/'));
          refresh();
        });
      });
      return true;
    }
    if (key(e, "u")) {   // update priority of the selected directory (or of this one)
      const json* sel = current();
      std::string dir_path = sel ? child_path(sel->value("name", "")) : path_;   // a file too
      set_priority(dir_path);
      return true;
    }
    const json* cur = current();
    if (!cur) return false;
    std::string target = child_path(cur->value("name", ""));
    bool dir = cur->value("dir", false);
    if (key(e, "t") || key(e, "T")) {
      tag_paths(ctx_, {target}, dir, key(e, "t"), [this] { refresh(); });
      return true;
    }
    if (key(e, "c") || key(e, "m")) {
      transfer_paths(ctx_, {target}, "", key(e, "m"));
      return true;
    }
    if (key(e, "S")) {   // keep this directory synced to another node, regularly
      schedule_dialog(ctx_, dir ? target : path_, nullptr);
      return true;
    }
    if (key(e, "f")) {   // download to this machine's fetch folder
      fetch_paths(ctx_, {target}, "");
      return true;
    }
    if (key(e, "x")) {   // pick up / put down this item
      if (!picked_.erase(target)) picked_.insert(target);
      rebuild_labels();
      return true;
    }
    if (key(e, "V")) {   // tolerate several versions of this file or everything below this folder
      ctx_.dialogs->ask_choice(
          "Several versions of " + target + (dir ? " (everything below it)" : "") + ":\n"
          "where the copies of a file differ, tolerate it: no decision is asked and nothing is "
          "overwritten.",
          {"tolerate several versions", "decide conflicts (the default)", "inherit from the parent"},
          [this, target](int i) {
            json value = i == 2 ? json(nullptr) : json(i == 0);
            ctx_.send("/api/tolerance", {{"path", target}, {"tolerant", value}},
                      [this, target](json out) {
                        ctx_.notify(target + ": " +
                                    (out.value("tolerant", false) ? "several versions are fine"
                                                                  : "conflicting versions are decided") +
                                    (out.value("inherited", false) ? " (inherited)" : ""));
                        refresh();
                      });
          });
      return true;
    }
    bool packed = cur->value("packed", false);
    if (key(e, "z") && dir) {   // pack a directory into one archive, or unpack it
      if (packed)
        unpack_dir(target);
      else
        pack_dir(target);
      return true;
    }
    if (key(e, "Z") && packed) {   // forget the archive without unpacking it
      ctx_.dialogs->confirm(
          "Drop the archive of " + target + "?\nIts files are NOT extracted: they are gone once "
          "the archive is deleted (it is only marked for deletion, you confirm in the Marked tab).",
          [this, target] {
            ctx_.send("/api/archives/drop", {{"vpath", target}}, [this, target](json) {
              ctx_.notify(target + " is no longer packed; its archive is marked for deletion");
              refresh();
            });
          });
      return true;
    }
    if (key(e, "D") && dir) {   // remove an empty directory
      ctx_.dialogs->confirm("Remove the empty directory " + target + "?", [this, target] {
        ctx_.send("/api/rmdir", {{"path", target}}, [this, target](json) {
          ctx_.notify("removed " + target);
          refresh();
        });
      });
      return true;
    }
    if (key(e, "n")) {   // rename in place
      ctx_.dialogs->ask_text("New name for " + target, cur->value("name", ""),
                             [this, target](const std::string& name) {
        if (name.empty()) return;
        move_items({target}, path_, name);
      });
      return true;
    }
    if (key(e, "v")) {   // move to a directory typed by the user
      std::vector<std::string> items(picked_.begin(), picked_.end());
      if (items.empty()) items = {target};
      std::string what = items.size() == 1 ? items[0] : std::to_string(items.size()) + " items";
      ctx_.dialogs->ask_text("Move " + what + " to the virtual directory:", path_,
                             [this, items](const std::string& dest) {
        if (!dest.empty()) move_items(items, dest, "");
      });
      return true;
    }
    return false;
  }

  void pack_dir(const std::string& target) {
    ctx_.dialogs->ask_choice(
        "Pack " + target + " into one squashfs archive (" + target + ".sqsh)?\n"
        "It then shows as a single entry; search looks inside only with 'deep search'.\n"
        "The directory must be on one node of a server with the squashfs tools.",
        {"pack, keep the original files", "pack, then remove the original files"},
        [this, target](int i) {
          ctx_.send("/api/archives/pack", {{"vpath", target}, {"remove_originals", i == 1}},
                    [this, target](json out) {
                      ctx_.notify("packing " + target + " on " + out.value("node", "") +
                                  " in the background (see the status line)");
                      follow_ = 5;
                    });
        });
  }

  void unpack_dir(const std::string& target) {
    ctx_.dialogs->confirm(
        "Unpack " + target + "?\nThe files are extracted next to the archive; the archive is "
        "then marked for deletion (you confirm that in the Marked tab).",
        [this, target] {
          ctx_.send("/api/archives/unpack", {{"vpath", target}}, [this, target](json out) {
            ctx_.notify("unpacking " + target + " on " + out.value("node", "") +
                        " in the background (see the status line)");
            follow_ = 5;
          });
        });
  }

  void set_priority(const std::string& dir_path) {
    ctx_.dialogs->ask_choice(
        "Update priority of " + dir_path + " (applies to everything below it):\n"
        "how often it is scanned for changes; archival is only scanned when you ask.\n"
        "A file can have its own setting too.",
        {"0  synchronous (a change is copied to the other nodes holding it at once)", "1  frequent",
         "2  infrequent (default)", "3  archival (never changes)", "inherit from the parent"},
        [this, dir_path](int i) {
          json value = i == 4 ? json(nullptr) : json(i);
          ctx_.send("/api/priority", {{"path", dir_path}, {"priority", value}},
                    [this, dir_path](json out) {
                      ctx_.notify(dir_path + ": " + out.value("name", "") +
                                  (out.value("inherited", false) ? " (inherited)" : ""));
                      refresh();
                    });
        });
  }

  void move_items(std::vector<std::string> items, const std::string& dest,
                  const std::string& name) {
    virtual_move(ctx_, std::move(items), dest, name, [this] {
      picked_.clear();
      follow_ = 5;
      refresh();
    });
  }

  Context& ctx_;
  std::set<std::string> picked_;   // vpaths picked up for a move (kept while browsing)
  int dir_priority_ = 2;           // update priority of the directory shown
  std::string this_node_;          // name of this server's own node
  std::string inside_;             // the packed directory being looked into ("" outside one)
  int more_files_ = 0;             // files of this folder beyond the listing limit
  int follow_ = 0;                 // polls left to refresh the listing after a move
  std::string path_ = "/";
  std::string want_;   // entry to select when the next listing arrives
  bool loading_ = false;
  json entries_ = json::array();       // what is listed (after the filter)
  json all_entries_ = json::array();   // the whole folder
  std::string filter_;                 // "/": only names containing this
  std::vector<std::string> labels_;
  int selected_ = 0;
  Element detail_ = text("");
  Component menu_, root_;
};

// --- Search -------------------------------------------------------------------------

class SearchView : public View {
 public:
  explicit SearchView(Context& ctx) : ctx_(ctx) {
    InputOption opt;
    opt.multiline = false;
    opt.on_enter = [this] { search(); };
    query_input_ = Input(&query_, "part of a name, e.g. report  (wildcards: *.jpg)", opt);
    tag_input_ = Input(&tag_, "tag", opt);
    auto issues = Checkbox("non-portable", &issues_);
    auto under = Checkbox("under-replicated", &under_);
    auto deep = Checkbox("deep search (inside packed dirs)", &deep_);
    auto button = Button("Search", [this] { search(); }, ButtonOption::Ascii());
    MenuOption mopt = MenuOption::Vertical();
    mopt.on_change = [this] { load_detail(); };
    mopt.on_enter = [this] { show_in_tree(); };
    menu_ = Menu(&labels_, &selected_, mopt);
    auto form = Container::Horizontal({query_input_, tag_input_, issues, under, deep, button});
    // Focusable line holding the keyboard focus while not typing, so that the
    // number keys keep switching tabs ("/" starts typing a search).
    hint_ = Renderer([this](bool focused) {
      auto line = text(" / to type a search (names of files and folders; a tag in the second "
                       "field)   tags: " + tags_line_);
      return focused ? line | bold : line | dim;
    });
    auto layout = Container::Vertical({hint_, form, menu_});
    root_ = Renderer(layout, [=, this] {
      auto form_el = hbox({
          query_input_->Render() | border | flex,
          tag_input_->Render() | border | size(WIDTH, EQUAL, 20),
          vbox({issues->Render(), under->Render(), deep->Render()}),
          button->Render() | vcenter,
      });
      std::string heading = "results";
      if (searching_) {
        heading = "searching...";
      } else if (!labels_.empty()) {
        heading = (dirs_total_ > 0 ? std::to_string(dirs_total_) + " folder(s), " : "") +
                  std::to_string(total_) + " file(s)" +
                  (members_.empty() ? ""
                                    : ", " + std::to_string(members_.size()) +
                                          (members_truncated_ ? "+" : "") + " inside packed dirs") +
                  (truncated_ ? " (first 1000 shown)" : "");
      }
      return vbox({form_el, hint_->Render(),
                   with_detail(list_panel(heading, menu_, labels_.size(),
                                          "type a pattern and/or a tag, then Enter"),
                               detail_) |
                       flex});
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Search"; }
  Component component() override { return root_; }
  bool typing() const override { return query_input_->Focused() || tag_input_->Focused(); }
  std::string help() const override {
    return "/: type a search  Enter: search, or show a result in the Tree  Esc: leave the fields  "
           "t/T: tag  c/m: copy/move  C/M: all";
  }

  void refresh() override {
    ctx_.get("/api/tags", [this](json out) {
      tags_line_.clear();
      for (const auto& t : out["tags"])
        tags_line_ += t.value("tag", "") + "(" + std::to_string(t.value("files", 0)) + ") ";
      if (tags_line_.empty()) tags_line_ = "(none)";
    }, "search-tags");
    if (searched_) search(false);
    leave_inputs();  // arriving on the tab: number keys still switch tabs
  }

 private:
  // Folders come first, then files, then (deep search) files inside packed directories:
  // row i is a folder if i < dirs_.size().
  int ndirs() const { return static_cast<int>(dirs_.size()); }
  bool on_dir() const { return selected_ >= 0 && selected_ < ndirs(); }
  const json* current_file() const {
    int i = selected_ - ndirs();
    return i >= 0 && i < static_cast<int>(results_.size()) ? &results_[i] : nullptr;
  }
  const json* current_member() const {
    int i = selected_ - ndirs() - static_cast<int>(results_.size());
    return i >= 0 && i < static_cast<int>(members_.size()) ? &members_[i] : nullptr;
  }
  // Path of the selected row (folder or file); "" if there is none.
  std::string current_path() const {
    if (on_dir()) return dirs_[selected_].value("path", "");
    const json* f = current_file();
    if (f) return f->value("path", "");
    const json* m = current_member();
    return m ? m->value("vpath", "") : "";
  }

  // Only while this tab is the one on screen: taking the focus of a hidden
  // tab would switch to it.
  void leave_inputs() {
    if (!root_->Focused()) return;
    if (!labels_.empty())
      menu_->TakeFocus();
    else
      hint_->TakeFocus();
  }

  // `from_user`: the user pressed Enter, so move on to the results when they come.
  void search(bool from_user = true) {
    std::string q = "/api/find?q=" + url_encode(query_) + "&tag=" + url_encode(tag_);
    if (issues_) q += "&issues=1";
    if (under_) q += "&under=1";
    if (deep_) q += "&deep=1";
    searched_ = true;
    searching_ = true;
    ctx_.get(q, [this, from_user](json out) {
      searching_ = false;
      results_ = out["files"];
      dirs_ = out.value("dirs", json::array());
      total_ = out.value("total", 0);
      dirs_total_ = out.value("dirs_total", 0);
      members_ = out.value("members", json::array());
      members_truncated_ = out.value("members_truncated", false);
      truncated_ = out.value("truncated", false) || dirs_total_ > ndirs();
      labels_.clear();
      for (const auto& d : dirs_)
        labels_.push_back(fit("▸ " + d.value("path", "") + "/", 50) + " " +
                          std::to_string(d.value("files", 0)) + " file(s)");
      for (const auto& f : results_) {
        json shown = f;
        shown["name"] = f.value("path", "");
        labels_.push_back("  " + file_label(shown, 48));
      }
      for (const auto& m : members_)
        labels_.push_back("▣ " + fit(m.value("vpath", ""), 60) + " " +
                          human_size(m.value("size", std::int64_t{0})));
      selected_ = std::min(selected_, std::max(0, static_cast<int>(labels_.size()) - 1));
      load_detail();
      if (from_user && typing()) leave_inputs();
    }, "search");
  }

  void load_detail() {
    if (on_dir()) {
      const json& d = dirs_[selected_];
      detail_ = vbox({hard_wrap(d.value("path", "") + "/", kDetailWidth - 1) | bold, separator(),
                      text("folder with " + std::to_string(d.value("files", 0)) +
                           " matching file(s)"),
                      text(""),
                      text("Enter: show it in the Tree") | dim,
                      text("t: tag every file below  c/m: copy/move it all") | dim});
      return;
    }
    if (const json* m = current_member()) {
      detail_ = vbox({hard_wrap(m->value("vpath", ""), kDetailWidth - 1) | bold, separator(),
                      text("inside the packed directory " + m->value("archive", "")),
                      text("size: " + human_size(m->value("size", std::int64_t{0}))), text(""),
                      text("Enter: show the packed directory in the Tree") | dim,
                      text("z (in the Tree): unpack it") | dim});
      return;
    }
    const json* cur = current_file();
    if (!cur) {
      detail_ = text("");
      return;
    }
    std::string path = cur->value("path", "");
    ctx_.run(
        false, [path](Api& a) { return a.get("/api/file?path=" + url_encode(path)); },
        [this, path](json f) {
          const json* now = current_file();
          if (!now || now->value("path", "") != path) return;   // selection moved on
          detail_ = file_details(f);
        },
        "search-detail");
  }

  // Open the selected result in the Tree: a folder itself, a file in its folder.
  void show_in_tree() {
    std::string path = current_path();
    if (path.empty()) return;
    if (const json* m = current_member()) {   // the packed directory it is in
      std::string dir = m->value("archive", "");
      auto cut = dir.rfind('/');
      ctx_.show_in_tree(cut == 0 || cut == std::string::npos ? "/" : dir.substr(0, cut),
                        dir.substr(cut + 1));
      return;
    }
    if (on_dir()) {
      ctx_.show_in_tree(path, "");
      return;
    }
    auto slash = path.rfind('/');
    ctx_.show_in_tree(slash == 0 || slash == std::string::npos ? "/" : path.substr(0, slash),
                      path.substr(slash + 1));
  }

  std::vector<std::string> all_paths() const {
    std::vector<std::string> out;
    for (const auto& d : dirs_) out.push_back(d.value("path", ""));
    for (const auto& f : results_) out.push_back(f.value("path", ""));
    return out;
  }

  bool on_event(const Event& e) {
    if (e == Event::Escape && typing()) {
      leave_inputs();  // the global keys (tabs, q) work again
      return true;
    }
    if (key(e, "/") && !typing()) {
      query_input_->TakeFocus();
      return true;
    }
    if (!menu_->Focused() || labels_.empty()) return false;
    std::string path = current_path();
    if (path.empty()) return false;
    if (current_member() && (key(e, "t") || key(e, "T") || key(e, "c") || key(e, "m") ||
                             key(e, "f"))) {
      ctx_.error("this file is inside a packed directory: act on the directory (Enter shows it "
                 "in the Tree), or unpack it");
      return true;
    }
    if (key(e, "t") || key(e, "T")) {
      tag_paths(ctx_, {path}, on_dir(), key(e, "t"), [this] { search(false); });
      return true;
    }
    if (key(e, "c") || key(e, "m")) {
      transfer_paths(ctx_, {path}, "", key(e, "m"));
      return true;
    }
    if (key(e, "f")) {
      fetch_paths(ctx_, {path}, "");
      return true;
    }
    if (key(e, "C") || key(e, "M")) {
      if (truncated_) {
        ctx_.error("too many results; narrow the search (or use the tag) first");
        return true;
      }
      // A pure tag search is sent as a tag selector; otherwise the paths.
      if (query_.empty() && !tag_.empty() && !issues_ && !under_)
        transfer_paths(ctx_, {}, tag_, key(e, "M"));
      else
        transfer_paths(ctx_, all_paths(), "", key(e, "M"));
      return true;
    }
    if (key(e, "r")) {
      search(false);
      return true;
    }
    return false;
  }

  Context& ctx_;
  bool searching_ = false;
  std::string query_, tag_, tags_line_;
  bool issues_ = false, under_ = false, deep_ = false, searched_ = false, truncated_ = false;
  bool members_truncated_ = false;
  int total_ = 0, dirs_total_ = 0;
  json members_ = json::array();   // deep search: files inside packed directories
  json results_ = json::array();
  json dirs_ = json::array();
  std::vector<std::string> labels_;
  int selected_ = 0;
  Element detail_ = text("");
  Component query_input_, tag_input_, hint_, menu_, root_;
};

// --- several servers ---------------------------------------------------------------

// Lists merged from every paired server carry "server" / "server_name".
std::string where_suffix(Context& ctx, const json& item) {
  std::string sid = item.value("server", "");
  if (sid.empty() || sid == ctx.api->server_id()) return "";
  return "  @" + item.value("server_name", sid.substr(0, 8));
}

// Address an action to the server that owns the item.
json on_server(json body, const json& item) {
  std::string sid = item.value("server", "");
  if (!sid.empty()) body["server"] = sid;
  return body;
}

std::string server_query(const json& item) {
  std::string sid = item.value("server", "");
  return sid.empty() ? "" : "server=" + url_encode(sid);
}

std::string unreachable_note(const json& out) {
  if (!out.contains("unreachable") || out["unreachable"].empty()) return "";
  return "  (unreachable: " + join(out["unreachable"]) + ")";
}

// --- Decisions ----------------------------------------------------------------------

class DecisionsView : public View {
 public:
  explicit DecisionsView(Context& ctx) : ctx_(ctx) {
    MenuOption opt = MenuOption::Vertical();
    opt.on_enter = [this] { decide_current(); };
    opt.on_change = [this] { load_members(); };
    menu_ = Menu(&labels_, &selected_, opt);
    root_ = Renderer(menu_, [this] {
      return with_detail(list_panel(std::to_string(items_.size()) +
                                        " decision(s) need attention" + note_,
                                    menu_, labels_.size(), "nothing needs your attention"),
                         detail());
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Decisions"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "Enter: decide  a: apply one choice to all of this kind  r: refresh";
  }

  void refresh() override {
    ctx_.get("/api/decisions?everywhere=1&limit=500", [this](json out) {
      std::string keep = current_key();
      items_ = out["decisions"];
      note_ = unreachable_note(out);
      int total = out.value("total", 0);
      if (total > static_cast<int>(items_.size()))
        note_ += (note_.empty() ? "" : "  ") + std::string("showing the oldest ") +
                 std::to_string(items_.size()) + " of " + std::to_string(total) +
                 " on this server; decide some to see more";
      labels_.clear();
      for (const auto& d : items_)
        labels_.push_back(fit("[" + d.value("kind", "") + "]", 16) + d.value("path", "") +
                          (d["node"].is_string() ? "  (" + d.value("node", "") + ")" : "") +
                          where_suffix(ctx_, d));
      selected_ = reselect(items_, keep, key_of, selected_);
      load_members();
    }, "decisions");
  }

 private:
  // A group stands for many decisions: show a few of the paths it is made of.
  void load_members() {
    members_.clear();
    members_of_.clear();
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return;
    const json& d = items_[selected_];
    if (!d.value("group", false)) return;
    std::string key = key_of(d);
    std::string path = "/api/decisions/" + std::to_string(d.value("id", 0)) + "/members?limit=12";
    if (auto q = server_query(d); !q.empty()) path += "&" + q;
    ctx_.get(path, [this, key](json out) {
      if (selected_ < 0 || selected_ >= static_cast<int>(items_.size()) ||
          key_of(items_[selected_]) != key)
        return;   // the selection moved on
      members_.clear();
      for (const auto& p : out["paths"]) members_.push_back(p.get<std::string>());
      members_of_ = key;
    }, "decision-members");
  }

  static std::string key_of(const json& d) { return d.value("server", "") + d["id"].dump(); }
  std::string current_key() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return "";
    return key_of(items_[selected_]);
  }

  Element detail() {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return text("");
    const json& d = items_[selected_];
    Elements rows = {hard_wrap(d.value("path", ""), kDetailWidth - 1) | bold, separator(),
                     label_value("kind", d.value("kind", "")),
                     label_value("server", d.value("server_name", ""))};
    rows.push_back(paragraph(d.value("summary", "")));
    if (d.value("group", false) && members_of_ == key_of(d) && !members_.empty()) {
      rows.push_back(separator());
      rows.push_back(text("for example") | bold);
      for (const auto& p : members_) rows.push_back(hard_wrap(p, kDetailWidth - 1) | dim);
      if (d.value("members", 0) > static_cast<int>(members_.size()))
        rows.push_back(text("... and " + std::to_string(d.value("members", 0) - static_cast<int>(members_.size())) +
                            " more") | dim);
    }
    rows.push_back(separator());
    rows.push_back(text("choices") | bold);
    for (const auto& o : d["options"])
      rows.push_back(paragraph("• " + o.value("label", "")));
    return vbox(rows);
  }

  void resolve(const json& d, const std::string& option) {
    ctx_.send("/api/decisions/" + std::to_string(d.value("id", 0)),
              on_server({{"option", option}}, d), [this](json out) {
                ctx_.notify(out.value("summary", "done"));
                refresh();
              });
  }

  void decide_current() {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return;
    json d = items_[selected_];
    std::vector<std::string> labels;
    for (const auto& o : d["options"]) labels.push_back(o.value("label", ""));
    ctx_.dialogs->ask_choice(d.value("summary", "") + "\n" + d.value("path", ""), labels,
                             [this, d](int i) { resolve(d, d["options"][i].value("key", "")); });
  }

  void decide_all_of_kind() {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return;
    json d = items_[selected_];
    std::string kind = d.value("kind", "");
    // Only offer choices that every decision of this kind has.
    std::vector<std::string> keys, labels;
    for (const auto& o : d["options"]) {
      bool everywhere = true;
      for (const auto& other : items_) {
        if (other.value("kind", "") != kind) continue;
        bool has = false;
        for (const auto& oo : other["options"]) has |= oo.value("key", "") == o.value("key", "");
        everywhere &= has;
      }
      if (everywhere) {
        keys.push_back(o.value("key", ""));
        labels.push_back(o.value("label", ""));
      }
    }
    int n = static_cast<int>(std::count_if(items_.begin(), items_.end(), [&](const json& x) {
      return x.value("kind", "") == kind;
    }));
    if (keys.empty()) {
      ctx_.error("these decisions have no choice in common; decide them one by one");
      return;
    }
    ctx_.dialogs->ask_choice(
        "Apply to all " + std::to_string(n) + " '" + kind + "' decision(s):", labels,
        [this, kind, keys, labels, n](int i) {
          ctx_.dialogs->confirm(
              "Really apply \"" + labels[i] + "\" to " + std::to_string(n) + " decision(s)?",
              [this, kind, key = keys[i]] {
                std::vector<json> targets;
                for (const auto& x : items_)
                  if (x.value("kind", "") == kind) targets.push_back(x);
                // One background job: the decisions are resolved one after the other.
                ctx_.run(
                    true,
                    [targets, key](Api& a) {
                      int done = 0, failed = 0;
                      std::string last;
                      for (const auto& x : targets) {
                        try {
                          a.post("/api/decisions/" + std::to_string(x.value("id", 0)),
                                 on_server({{"option", key}}, x));
                          ++done;
                        } catch (const std::exception& e) {
                          ++failed;
                          last = e.what();
                        }
                      }
                      return json{{"done", done}, {"failed", failed}, {"last", last}};
                    },
                    [this](json out) {
                      ctx_.notify("applied to " + std::to_string(out.value("done", 0)) +
                                  " decision(s)");
                      if (out.value("failed", 0) > 0)
                        ctx_.error(std::to_string(out.value("failed", 0)) + " failed: " +
                                   out.value("last", ""));
                      refresh();
                    });
              });
        });
  }

  bool on_event(const Event& e) {
    if (key(e, "r")) return refresh(), true;
    if (key(e, "a")) return decide_all_of_kind(), true;
    return false;
  }

  std::vector<std::string> members_;   // sample paths of the selected group
  std::string members_of_;             // key of the group they belong to
  Context& ctx_;
  std::set<std::string> marked_;   // nodes chosen with Space for the next scan
  json items_ = json::array();
  std::string note_;
  std::vector<std::string> labels_;
  int selected_ = 0;
  Component menu_, root_;
};

// --- Wanted -------------------------------------------------------------------------

class WantedView : public View {
 public:
  explicit WantedView(Context& ctx) : ctx_(ctx) {
    menu_ = Menu(&labels_, &selected_, MenuOption::Vertical());
    root_ = Renderer(menu_, [this] {
      std::string shown = total_ > static_cast<int>(items_.size())
                              ? " (showing " + std::to_string(items_.size()) + " of " +
                                    std::to_string(total_) + "; A approves all)"
                              : "";
      return vbox({list_panel(std::to_string(items_.size()) + " proposed job(s), " +
                                  std::to_string(checked_.size()) + " selected" + shown + note_,
                              menu_, labels_.size(), "no proposed jobs")});
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Wanted"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "Space: select  a/x: approve/reject selected  A/X: approve/reject all  r: refresh";
  }

  void refresh() override {
    ctx_.get("/api/wanted?everywhere=1", [this](json out) {
      items_ = out["wanted"];
      total_ = std::max(out.value("total", 0), static_cast<int>(items_.size()));
      note_ = unreachable_note(out);
      std::set<std::string> still;
      for (const auto& w : items_)
        if (checked_.count(key_of(w))) still.insert(key_of(w));
      checked_ = still;
      relabel();
      selected_ = std::min(selected_, std::max(0, static_cast<int>(items_.size()) - 1));
    }, "wanted");
  }

 private:
  static std::string key_of(const json& w) { return w.value("server", "") + ":" + w["id"].dump(); }

  void relabel() {
    labels_.clear();
    for (const auto& w : items_) {
      bool on = checked_.count(key_of(w)) > 0;
      std::string route = w.value("from", "").empty() ? "→ " + w.value("to", "")
                                                      : w.value("from", "") + " → " + w.value("to", "");
      labels_.push_back(std::string(on ? "[x] " : "[ ] ") + fit(w.value("op", ""), 10) +
                        fit(w.value("path", ""), 50) + " " + fit(route, 24) + " " +
                        w.value("reason", "") + where_suffix(ctx_, w) +
                        (w.value("message", "").empty() ? "" : "  (" + w.value("message", "") + ")"));
    }
  }

  // Selected items (or the current one), grouped by server: ids are per server.
  std::map<std::string, std::vector<int>> targets() const {
    std::map<std::string, std::vector<int>> out;
    for (size_t i = 0; i < items_.size(); ++i) {
      const json& w = items_[i];
      bool pick = checked_.empty() ? static_cast<int>(i) == selected_ : checked_.count(key_of(w)) > 0;
      if (pick) out[w.value("server", "")].push_back(w.value("id", 0));
    }
    return out;
  }

  void act(const std::string& action, bool all) {
    std::map<std::string, std::vector<int>> groups;
    if (all) {
      for (const auto& w : items_) groups[w.value("server", "")];
    } else {
      groups = targets();
    }
    if (groups.empty()) return;
    checked_.clear();
    relabel();
    // One background job: one request per server, one after the other.
    ctx_.run(
        true,
        [action, all, groups](Api& a) {
          int jobs = 0, items = 0, rejected = 0;
          json errors = json::array();
          for (const auto& [server, ids] : groups) {
            json body = {{"action", action}};
            if (all)
              body["all"] = true;
            else
              body["ids"] = ids;
            if (!server.empty()) body["server"] = server;
            try {
              json out = a.post("/api/wanted", body);
              if (out.contains("job_id") && !out["job_id"].is_null()) ++jobs;
              jobs += out.value("remote_jobs", 0);
              items += out.value("items", 0);
              rejected += out.value("rejected", 0);
              for (const auto& err : out.value("errors", json::array())) errors.push_back(err);
            } catch (const std::exception& e) {
              errors.push_back(e.what());
            }
          }
          return json{{"jobs", jobs}, {"items", items}, {"rejected", rejected},
                      {"errors", errors}};
        },
        [this, action](json out) {
          if (action == "approve")
            ctx_.notify(std::to_string(out.value("jobs", 0)) + " job(s) created with " +
                        std::to_string(out.value("items", 0)) + " item(s)");
          else
            ctx_.notify("rejected " + std::to_string(out.value("rejected", 0)) + " proposal(s)");
          for (const auto& err : out["errors"]) ctx_.error(err.get<std::string>());
          refresh();
        });
  }

  bool on_event(const Event& e) {
    if (key(e, " ")) {
      if (selected_ >= 0 && selected_ < static_cast<int>(items_.size())) {
        std::string k = key_of(items_[selected_]);
        if (!checked_.erase(k)) checked_.insert(k);
        relabel();
        if (selected_ + 1 < static_cast<int>(items_.size())) ++selected_;
      }
      return true;
    }
    if (key(e, "a")) return act("approve", false), true;
    if (key(e, "x")) return act("reject", false), true;
    if (key(e, "A")) {
      ctx_.dialogs->confirm("Approve all " + std::to_string(items_.size()) + " proposals?",
                            [this] { act("approve", true); });
      return true;
    }
    if (key(e, "X")) {
      ctx_.dialogs->confirm("Reject all " + std::to_string(items_.size()) + " proposals?",
                            [this] { act("reject", true); });
      return true;
    }
    if (key(e, "r")) return refresh(), true;
    return false;
  }

  Context& ctx_;
  json items_ = json::array();
  std::string note_;
  int total_ = 0;
  std::set<std::string> checked_;
  std::vector<std::string> labels_;
  int selected_ = 0;
  Component menu_, root_;
};

// --- Jobs ----------------------------------------------------------------------------

class JobsView : public View {
 public:
  explicit JobsView(Context& ctx) : ctx_(ctx) {
    MenuOption opt = MenuOption::Vertical();
    opt.on_change = [this] { load_detail(); };
    menu_ = Menu(&labels_, &selected_, opt);
    root_ = Renderer(menu_, [this] {
      return with_detail(list_panel((all_ ? "all jobs" : "active jobs") + note_, menu_,
                                    labels_.size(),
                                    all_ ? "no jobs" : "no active jobs (h: show finished)"),
                         detail_);
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Jobs"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "j: run now  c: cancel  y: retry failed/skipped  h: show/hide finished  r: refresh";
  }
  void on_poll() override {
    if (++polls_ % 2 == 0) refresh();  // every ~4 s: lists come from every server
  }

  void refresh() override {
    ctx_.get(all_ ? "/api/jobs?all=1&everywhere=1" : "/api/jobs?everywhere=1", [this](json out) {
      std::string keep = current_key();
      items_ = out["jobs"];
      note_ = unreachable_note(out);
      labels_.clear();
      for (const auto& j : items_)
        labels_.push_back(fit("#" + std::to_string(j.value("id", 0)), 6) +
                          fit(j.value("status", ""), 10) +
                          fit(std::to_string(j.value("done", 0)) + "/" +
                                  std::to_string(j.value("total", 0)), 12) +
                          (j.value("issues", 0) > 0
                               ? "[" + std::to_string(j.value("issues", 0)) + " NOT done] "
                               : std::string()) +
                          j.value("description", "") + where_suffix(ctx_, j));
      selected_ = reselect(items_, keep, key_of, selected_);
      load_detail();
    }, "jobs");
  }

 private:
  static std::string key_of(const json& j) { return j.value("server", "") + j["id"].dump(); }
  const json* current() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return nullptr;
    return &items_[selected_];
  }
  std::string current_key() const { return current() ? key_of(*current()) : ""; }

  void load_detail() {
    const json* cur = current();
    if (!cur) {
      detail_ = text("");
      return;
    }
    std::string wanted = key_of(*cur), q = server_query(*cur), server_name = cur->value("server_name", "");
    std::string path = "/api/jobs/" + std::to_string(cur->value("id", 0)) + (q.empty() ? "" : "?" + q);
    ctx_.get(path, [this, wanted, server_name](json j) {
      const json* now = current();
      if (!now || key_of(*now) != wanted) return;   // selection moved on
      show_detail(j, server_name);
    }, "jobs-detail");
  }

  void show_detail(const json& j, const std::string& server_name) {
    Elements rows = {paragraph(j.value("description", "")) | bold, separator(),
                     label_value("status", j.value("status", "")),
                     label_value("server", server_name),
                     label_value("created", j.value("created", "").substr(0, 19))};
    std::string counts;
    for (auto& [k, v] : j["counts"].items()) counts += k + " " + v.dump() + "  ";
    rows.push_back(label_value("items", counts));
    if (j.value("issues", 0) > 0)
      rows.push_back(paragraph(std::to_string(j.value("issues", 0)) +
                               " item(s) were NOT done (listed below), so this job is not "
                               "complete.") |
                     color(Color::Red) | bold);
    if (!j["items"].empty()) {
      rows.push_back(separator());
      rows.push_back(text("items not done") | bold);
    }
    int shown = 0;
    for (const auto& it : j["items"]) {
      if (++shown > 200) {
        rows.push_back(text("...") | dim);
        break;
      }
      std::string state = it.value("state", "");
      Color c = Color::Default;
      if (state == "failed" || state == "conflict" || state == "skipped")
        c = Color::Red;
      else if (state == "pending")
        c = Color::Yellow;
      rows.push_back(hbox({text(fit(state, 10)) | color(c), paragraph(it.value("path", ""))}));
      if (!it.value("message", "").empty())
        rows.push_back(hbox({text("  "), paragraph(it.value("message", "")) | dim}));
    }
    detail_ = vbox(rows);
  }

  bool on_event(const Event& e) {
    if (key(e, "r")) return refresh(), true;
    if (key(e, "h")) {
      all_ = !all_;
      refresh();
      return true;
    }
    if (key(e, "j")) {
      // Every server runs its own jobs: ask each one shown.
      std::set<std::string> servers = {""};
      for (const auto& j : items_) servers.insert(j.value("server", ""));
      std::string me = ctx_.api->server_id();
      ctx_.run(
          true,
          [servers, me](Api& a) {
            for (const auto& sid : servers) {
              json body = json::object();
              if (!sid.empty() && sid != me) body["server"] = sid;
              else if (!sid.empty()) continue;
              try {
                a.post("/api/jobs/run", body);
              } catch (const std::exception&) {   // an unreachable server is not fatal
              }
            }
            return json::object();
          },
          [this](json) { ctx_.notify("job run queued"); });
      return true;
    }
    const json* cur = current();
    if (!cur) return false;
    json item = *cur;
    std::string id = std::to_string(item.value("id", 0));
    if (key(e, "c")) {
      ctx_.dialogs->confirm("Cancel job #" + id + "? Items already done stay done.",
                            [this, id, item] {
                              ctx_.send("/api/jobs/" + id + "/cancel",
                                        on_server(json::object(), item), [this, id](json) {
                                          ctx_.notify("job #" + id + " cancelled");
                                          refresh();
                                        });
                            });
      return true;
    }
    if (key(e, "y")) {
      ctx_.send("/api/jobs/" + id + "/retry", on_server(json::object(), item), [this](json out) {
        ctx_.notify("re-queued " + std::to_string(out.value("items", 0)) + " failed item(s)");
        refresh();
      });
      return true;
    }
    return false;
  }

  Context& ctx_;
  bool all_ = false;
  int polls_ = 0;
  json items_ = json::array();
  std::string note_;
  std::vector<std::string> labels_;
  int selected_ = 0;
  Element detail_ = text("");
  Component menu_, root_;
};

// --- Nodes ---------------------------------------------------------------------------

class NodesView : public View {
 public:
  explicit NodesView(Context& ctx) : ctx_(ctx) {
    MenuOption opt = MenuOption::Vertical();
    // The servers are the trunks of the tree: bold, and not dimmed like the branches.
    opt.entries_option.transform = [this](const EntryState& st) {
      Element e = text((st.active ? "> " : "  ") + st.label);
      bool header = st.index >= 0 && st.index < static_cast<int>(rows_.size()) &&
                    rows_[st.index].kind != Row::Node && rows_[st.index].kind != Row::Foreign;
      if (header) e = e | bold;
      if (st.focused) e = e | inverted;
      if (st.active) e = e | bold;
      if (!st.focused && !st.active && !header) e = e | dim;
      return e;
    };
    menu_ = Menu(&labels_, &selected_, opt);
    root_ = Renderer(menu_, [this] {
      return with_detail(list_panel("servers and the nodes they own", menu_, labels_.size(),
                                    "no nodes"),
                         detail());
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Nodes"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "Space: choose  s: scan the chosen (or this) node  S: scan all  w: new files only  v: verify  c: check completeness  m: move root  n: add drive  "
           "R: rename  p: priority  t: backup target  B: backup first  M: mirrored drive  o: adopt";
  }

  void refresh() override {
    ctx_.get("/api/nodes", [this](json out) {
      std::string keep = current_key();
      items_ = out["nodes"];
      foreign_ = out.value("foreign", json::array());
      rebuild_labels();
      selected_ = reselect_row(keep);
    }, "nodes");
  }

 private:
  // One row of the list: a server (the trunk), a node it owns (a branch), or a foreign drive.
  struct Row {
    enum Kind { Server, Node, Foreign, ForeignHeader } kind = Node;
    int idx = -1;           // items_ / foreign_ index (Node, Foreign)
    std::string owner_id;   // Server: whose; Node: whose it is
  };

  std::string state_of(const json& n) const {
    return n.value("online", false) ? (n.value("reachable", true) ? "online" : "unreachable")
                                    : "offline";
  }

  // The servers with the nodes they own, drawn as a tree:
  //   ▾ this server
  //      ├─ node
  //      └─ node
  // "»" marks the nodes chosen for a scan (Space).
  void rebuild_labels() {
    labels_.clear();
    rows_.clear();
    std::vector<std::string> order;                       // owner ids, this server first
    std::map<std::string, std::vector<int>> owned;
    std::map<std::string, json> server_info;
    for (size_t i = 0; i < items_.size(); ++i) {
      const json& n = items_[i];
      std::string id = n.value("owner_id", "");
      if (!owned.count(id)) {
        order.push_back(id);
        server_info[id] = {{"name", n.value("owner", "") == "self" ? ctx_.api->server_name()
                                                                   : n.value("owner", "")},
                           {"self", n.value("owner", "") == "self"},
                           {"online", n.value("owner", "") == "self" || n.value("owner_online", true)}};
      }
      owned[id].push_back(static_cast<int>(i));
    }
    std::stable_sort(order.begin(), order.end(), [&](const std::string& a, const std::string& b) {
      bool sa = server_info[a].value("self", false), sb = server_info[b].value("self", false);
      if (sa != sb) return sa;
      return server_info[a].value("name", "") < server_info[b].value("name", "");
    });
    for (const auto& id : order) {
      const json& info = server_info[id];
      std::int64_t bytes = 0;
      int files = 0;
      for (int i : owned[id]) {
        files += items_[i].value("files", 0);
        bytes += items_[i].value("bytes", std::int64_t{0});
      }
      std::string tag = info.value("self", false)       ? "this server"
                        : info.value("online", true) ? "online" : "UNREACHABLE";
      labels_.push_back("▾ " + info.value("name", "") + "  (" + tag + ")  " +
                        std::to_string(owned[id].size()) + " node(s), " + std::to_string(files) +
                        " files, " + human_size(bytes));
      rows_.push_back({Row::Server, -1, id});
      for (size_t k = 0; k < owned[id].size(); ++k) {
        const json& n = items_[owned[id][k]];
        bool last = k + 1 == owned[id].size();
        labels_.push_back(std::string(marked_.count(n.value("name", "")) ? "»" : " ") + " " +
                          (last ? "└─ " : "├─ ") + fit(n.value("name", ""), 16) +
                          fit(n.value("kind", ""), 9) + fit(state_of(n), 12) +
                          fit(std::to_string(n.value("files", 0)) + " files", 14) +
                          human_size(n.value("bytes", std::int64_t{0})));
        rows_.push_back({Row::Node, owned[id][k], id});
      }
    }
    if (!foreign_.empty()) {
      labels_.push_back("▾ attached here, owned by other servers (press o to adopt)");
      rows_.push_back({Row::ForeignHeader, -1, ""});
      for (size_t k = 0; k < foreign_.size(); ++k) {
        const json& f = foreign_[k];
        labels_.push_back(std::string("  ") + (k + 1 == foreign_.size() ? "└─ " : "├─ ") +
                          fit(f.value("name", ""), 16) + fit("foreign", 9) + fit("attached", 12) +
                          "owned by " + f.value("owner", ""));
        rows_.push_back({Row::Foreign, static_cast<int>(k), ""});
      }
    }
  }

  const Row* row() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(rows_.size())) return nullptr;
    return &rows_[selected_];
  }
  // Which row is "the same" after a refresh: a node or foreign drive by name, a server by id.
  std::string current_key() const {
    const Row* r = row();
    if (!r) return "";
    if (r->kind == Row::Node) return "n:" + items_[r->idx].value("name", "");
    if (r->kind == Row::Foreign) return "f:" + foreign_[r->idx].value("name", "");
    return "s:" + r->owner_id;
  }
  int reselect_row(const std::string& keep) const {
    for (size_t i = 0; i < rows_.size(); ++i) {
      const Row& r = rows_[i];
      std::string k = r.kind == Row::Node ? "n:" + items_[r.idx].value("name", "")
                      : r.kind == Row::Foreign ? "f:" + foreign_[r.idx].value("name", "")
                                               : "s:" + r.owner_id;
      if (k == keep) return static_cast<int>(i);
    }
    return std::clamp(selected_, 0, std::max(0, static_cast<int>(rows_.size()) - 1));
  }
  // Names of the nodes a server owns (what a scan on its row covers).
  std::vector<std::string> nodes_of(const std::string& owner_id) const {
    std::vector<std::string> out;
    for (const auto& n : items_)
      if (n.value("owner_id", "") == owner_id) out.push_back(n.value("name", ""));
    return out;
  }

  // The nodes to scan: the ones marked with Space, else the selected node (or, on a
  // server's row, all the nodes it owns).
  std::vector<std::string> scan_targets(const std::string& selected) {
    std::vector<std::string> out;
    for (const auto& n : items_)
      if (marked_.count(n.value("name", ""))) out.push_back(n.value("name", ""));
    if (out.empty()) {
      const Row* r = row();
      if (r && r->kind == Row::Server) return nodes_of(r->owner_id);
      out.push_back(selected);
    }
    return out;
  }
  static std::string joined(const std::vector<std::string>& names) {
    std::string out;
    for (const auto& n : names) out += (out.empty() ? "" : ", ") + n;
    return out;
  }

  bool on_foreign() const { return row() && row()->kind == Row::Foreign; }
  std::string current_name() const {
    const Row* r = row();
    if (!r || r->kind != Row::Node) return "";
    return items_[r->idx].value("name", "");
  }
  int node_index() const { return row() && row()->kind == Row::Node ? row()->idx : 0; }

  Element detail() {
    const Row* r = row();
    if (r && r->kind == Row::Server) {
      auto names = nodes_of(r->owner_id);
      std::int64_t bytes = 0;
      int files = 0;
      bool self = false;
      std::string name, state = "online";
      for (const auto& n : items_) {
        if (n.value("owner_id", "") != r->owner_id) continue;
        files += n.value("files", 0);
        bytes += n.value("bytes", std::int64_t{0});
        self = n.value("owner", "") == "self";
        name = self ? ctx_.api->server_name() : n.value("owner", "");
        if (!self && !n.value("owner_online", true)) state = "UNREACHABLE";
      }
      return vbox({text(name) | bold, separator(),
                   label_value("server", self ? "this server" : state),
                   label_value("nodes", joined(names)),
                   label_value("files", std::to_string(files) + " (" + human_size(bytes) + ")"),
                   text(""), text("s/w/v: scan (all its nodes)  Space: choose them all") | dim});
    }
    if (r && r->kind == Row::ForeignHeader)
      return paragraph("Drives of other servers that are attached to this machine. Select one and "
                       "press o to make this server its owner.") | dim;
    if (on_foreign()) {
      const json& f = foreign_[r->idx];
      return vbox({text(f.value("name", "")) | bold, separator(),
                   label_value("owner", f.value("owner", "")),
                   label_path("path", f.value("path", "")),
                   label_value("seen", f.value("seen", "").substr(0, 19)), text(""),
                   paragraph("This drive belongs to another server and is attached here. "
                             "Press o to make this server its owner.") | dim});
    }
    if (!r || r->kind != Row::Node) return text("");
    const json& n = items_[r->idx];
    auto yes = [](bool b) { return std::string(b ? "yes" : "no"); };
    return vbox({
        text(n.value("name", "")) | bold,
        separator(),
        label_value("kind", n.value("kind", "")),
        label_value("state", n.value("online", false) ? "online" : "offline"),
        label_value("owner", n.value("owner", "")),
        label_path("root", n.value("root", "")),
        label_value("files", std::to_string(n.value("files", 0)) + " (" +
                                 human_size(n.value("bytes", std::int64_t{0})) + ")"),
        label_value("case-sensitive", yes(n.value("case_sensitive", false))),
        label_value("Windows names", yes(n.value("restricted_names", false))),
        label_value("priority", std::to_string(n.value("priority", 0))),
        label_value("backup target", yes(n.value("replication_target", false))),
        label_value("backup first", yes(n.value("backup_first", false))),
        label_value("mirrored drive", yes(n.value("mirrored", false)) ),
        label_value("last scan", n.value("last_scan", "").substr(0, 19)),
    });
  }

  bool on_event(const Event& e) {
    if (key(e, "r")) return refresh(), true;
    if (key(e, "S")) {
      ctx_.send("/api/scan", {}, [this](json) { ctx_.notify("scan of all nodes queued"); });
      return true;
    }
    if (key(e, "n")) {
      ctx_.dialogs->ask_text(
          "Add a drive: the path of its folder ON THE SERVER'S MACHINE (e.g. /media/me/SSD)", "",
          [this](const std::string& path) {
            if (path.empty()) return;
            // Default name: the folder's name.
            std::string trimmed = path;
            while (trimmed.size() > 1 && (trimmed.back() == '/' || trimmed.back() == '\\'))
              trimmed.pop_back();
            auto slash = trimmed.find_last_of("/\\");
            std::string suggestion =
                slash == std::string::npos ? trimmed : trimmed.substr(slash + 1);
            ctx_.dialogs->ask_text("Name of the new node", suggestion,
                                   [this, path](const std::string& name) {
                                     if (name.empty()) return;
                                     ctx_.send("/api/nodes", {{"path", path}, {"name", name}},
                                               [this, name](json out) {
                                                 ctx_.notify("added node " +
                                                             out.value("name", name) +
                                                             "; scanning it");
                                                 refresh();
                                               });
                                   });
          });
      return true;
    }
    if (key(e, "o")) {
      if (!on_foreign()) {
        ctx_.error("select a foreign drive (listed after the nodes) to adopt it");
        return true;
      }
      json f = foreign_[row()->idx];
      ctx_.dialogs->confirm("Make this server the owner of '" + f.value("name", "") +
                                "' (owned by " + f.value("owner", "") + ")?",
                            [this, f] {
                              ctx_.send("/api/nodes/" + url_encode(f.value("node_id", "")) +
                                            "/adopt",
                                        {}, [this, f](json) {
                                          ctx_.notify("adopted " + f.value("name", "") +
                                                      "; scanning it");
                                          refresh();
                                        });
                            });
      return true;
    }
    std::string name = current_name();
    const Row* sel = row();
    bool on_server = sel && sel->kind == Row::Server;
    if (name.empty() && !on_server) return false;
    // Scanning keys also work on a server's row (all the nodes it owns); the rest need one node.
    if (name.empty() && !(key(e, " ") || key(e, "s") || key(e, "w") || key(e, "v"))) {
      if (key(e, "R") || key(e, "c") || key(e, "m") || key(e, "p") || key(e, "t")) {
        ctx_.error("select a node (a branch), not a server");
        return true;
      }
      return false;
    }
    if (key(e, "R")) {
      ctx_.dialogs->ask_text("New name for node " + name, name,
                             [this, name](const std::string& value) {
                               if (value.empty() || value == name) return;
                               ctx_.send("/api/nodes/" + url_encode(name), {{"name", value}},
                                         [this, name, value](json) {
                                           ctx_.notify("renamed " + name + " to " + value);
                                           refresh();
                                         });
                             });
      return true;
    }
    if (key(e, " ")) {   // choose nodes to scan together (on a server: all of its nodes)
      auto names = on_server ? nodes_of(sel->owner_id) : std::vector<std::string>{name};
      bool all_marked = std::all_of(names.begin(), names.end(),
                                    [this](const std::string& n) { return marked_.count(n) > 0; });
      for (const auto& n : names) {
        if (all_marked) marked_.erase(n); else marked_.insert(n);
      }
      rebuild_labels();
      return true;
    }
    if (key(e, "s")) {
      auto targets = scan_targets(name);
      ctx_.send("/api/scan", {{"nodes", targets}}, [this, targets](json) {
        ctx_.notify("scan of " + joined(targets) + " queued");
        marked_.clear();
        rebuild_labels();
      });
      return true;
    }
    if (key(e, "c")) {   // does the index account for everything on the disk?
      ctx_.notify("checking " + name + " (reads the whole folder; may take a while)...");
      ctx_.get("/api/nodes/" + url_encode(name) + "/audit", [this, name](json out) {
        std::string body = std::to_string(out.value("files_on_disk", 0)) + " files on disk, " +
                           std::to_string(out.value("files_indexed", 0)) + " in the index.\n";
        auto list = [&body](const json& items, const char* lead) {
          for (const auto& it : items) {
            body += "\n" + std::to_string(it.value("count", 0)) + " " + lead +
                    it.value("reason", "") + "\n";
            int shown = 0;
            for (const auto& ex : it["examples"]) {
              if (++shown > 5) break;
              body += "    " + ex.get<std::string>() + "\n";
            }
          }
        };
        list(out["not_in_index"], "not in the index: ");
        list(out["other"], "");
        const json gone = out.value("listed_but_not_on_disk", json::object());
        if (gone.value("count", 0) > 0) {
          body += "\n" + std::to_string(gone.value("count", 0)) +
                  " listed in the index but NOT on disk:\n";
          int shown = 0;
          for (const auto& ex : gone["examples"]) {
            if (++shown > 5) break;
            body += "    " + ex.get<std::string>() + "\n";
          }
        }
        if (out["not_in_index"].empty() && out["other"].empty() && gone.value("count", 0) == 0)
          body += "\nEverything on disk is in the index, and the other way round.";
        else
          body += "\nA scan (s) indexes new files; the rest is left out on purpose "
                  "(see [scan] in the config).";
        ctx_.dialogs->info("Check of " + name, body);
      });
      return true;
    }
    if (key(e, "m")) {   // change where the node's folder is
      std::string root = items_[node_index()].value("root", "");
      ctx_.dialogs->ask_text(
          "New place of the folder of " + name + " (a path ON THE SERVER'S MACHINE):", root,
          [this, name, root](const std::string& path) {
            if (path.empty() || path == root) return;
            ctx_.dialogs->ask_choice(
                "Folder of " + name + " -> " + path,
                {"It is already there (the folder was moved, or the drive is mounted elsewhere)",
                 "Move the folder there for me (same file system; nothing is copied or deleted)",
                 "Cancel"},
                [this, name, path](int i) {
                  if (i == 2) return;
                  ctx_.send("/api/nodes/" + url_encode(name) + "/root",
                            {{"path", path}, {"move", i == 1}}, [this, name](json out) {
                              ctx_.notify(name + " is now at " + out.value("path", ""));
                              refresh();
                            });
                });
          });
      return true;
    }
    if (key(e, "w")) {   // discover new files only
      auto targets = scan_targets(name);
      ctx_.send("/api/scan", {{"nodes", targets}, {"new_only", true}}, [this, targets](json) {
        ctx_.notify("scan for new files on " + joined(targets) + " queued");
        marked_.clear();
        rebuild_labels();
      });
      return true;
    }
    if (key(e, "v")) {
      auto targets = scan_targets(name);
      ctx_.dialogs->confirm("Re-hash every file on " + joined(targets) +
                                "? This reads all their data.",
                            [this, targets] {
                              ctx_.send("/api/scan", {{"nodes", targets}, {"verify", true}},
                                        [this, targets](json) {
                                          ctx_.notify("verify scan of " + joined(targets) +
                                                      " queued");
                                          marked_.clear();
                                          rebuild_labels();
                                        });
                            });
      return true;
    }
    if (key(e, "p")) {
      std::string current = std::to_string(items_[node_index()].value("priority", 0));
      ctx_.dialogs->ask_text("Priority of " + name + " (higher = preferred backup target)",
                             current, [this, name](const std::string& value) {
                               int n = 0;
                               try {
                                 n = std::stoi(value);
                               } catch (...) {
                                 ctx_.error("not a number: " + value);
                                 return;
                               }
                               ctx_.send("/api/nodes/" + url_encode(name), {{"priority", n}},
                                         [this](json) { refresh(); });
                             });
      return true;
    }
    if (key(e, "B") || key(e, "M")) {   // flags: backup first / mirrored drive
      std::string flag = key(e, "B") ? "backup_first" : "mirrored";
      bool now = items_[node_index()].value(flag, false);
      ctx_.send("/api/nodes/" + url_encode(name), {{flag, !now}}, [this, flag, now, name](json) {
        ctx_.notify(name + (flag == "mirrored"
                                ? (now ? ": no longer a mirrored drive"
                                       : ": mirrored drive (its copies count as two)")
                                : (now ? ": no longer filled first by backups"
                                       : ": backups fill it first")));
        refresh();
      });
      return true;
    }
    if (key(e, "t")) {
      bool target = items_[node_index()].value("replication_target", true);
      ctx_.send("/api/nodes/" + url_encode(name), {{"replication_target", !target}},
                [this](json) { refresh(); });
      return true;
    }
    return false;
  }

  Context& ctx_;
  std::set<std::string> marked_;   // nodes chosen with Space for the next scan
  std::vector<Row> rows_;          // what each line of the list stands for
  json items_ = json::array();
  json foreign_ = json::array();
  std::vector<std::string> labels_;
  int selected_ = 0;
  Component menu_, root_;
};

// --- Servers (pairing) -----------------------------------------------------------

class ServersView : public View {
 public:
  explicit ServersView(Context& ctx) : ctx_(ctx) {
    menu_ = Menu(&labels_, &selected_, MenuOption::Vertical());
    root_ = Renderer(menu_, [this] {
      Element self = vbox({
          hbox({text("This server: ") | dim, text(self_.value("name", "")) | bold}),
          hbox({text("fingerprint: ") | dim, text(self_.value("fingerprint", ""))}),
      });
      return vbox({self, separator(),
                   with_detail(list_panel("paired servers and pairing requests", menu_,
                                          labels_.size(), "not paired with any server (p: pair)"),
                               detail()) |
                       flex});
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Servers"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "p: pair  a/x: accept/reject  d: unpair  t: transport daemon/ssh  k: SSH key  s: sync";
  }
  void on_poll() override {
    if (++polls_ % 3 == 0) refresh();
  }

  void refresh() override {
    ctx_.get("/api/peers", [this](json out) { apply(out); }, "servers");
  }

 private:
  void apply(const json& out) {
    self_ = out["self"];
    rows_ = json::array();
    for (auto r : out["requests"]) {
      r["row"] = "request";
      rows_.push_back(r);
    }
    for (auto p : out["peers"]) {
      p["row"] = "peer";
      rows_.push_back(p);
    }
    labels_.clear();
    for (const auto& r : rows_) {
      if (r["row"] == "request") {
        bool in = r.value("direction", "") == "in";
        labels_.push_back(fit(in ? "▶ request" : "◀ requested", 13) + fit(r.value("name", ""), 16) +
                          fit(r.value("address", ""), 24) +
                          (in ? "press a to accept, x to reject" : "waiting for them to accept"));
      } else {
        std::string state = r.value("online", false)
                                ? (r.value("sync_note", "").empty() ? "online" : "catching up")
                                : "unreachable";
        // Why it is unreachable, right in the list (the details show it in full).
        std::string why = !r.value("online", false) && !r.value("last_error", "").empty()
                              ? "  ! " + r.value("last_error", "") : "";
        labels_.push_back(fit("paired", 13) + fit(r.value("name", ""), 16) +
                          fit(r.value("address", ""), 24) + fit(state, 12) +
                          fit(r.value("transport", "daemon"), 8) +
                          r.value("last_sync", "").substr(0, 19) + why);
      }
    }
    selected_ = std::min(selected_, std::max(0, static_cast<int>(rows_.size()) - 1));
  }


  const json* current() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(rows_.size())) return nullptr;
    return &rows_[selected_];
  }

  Element detail() {
    const json* r = current();
    if (!r) return text("");
    Elements rows = {text(r->value("name", "")) | bold, separator(),
                     label_value("address", r->value("address", "")),
                     label_value("fingerprint", hard_wrap(r->value("fingerprint", ""),
                                                          kDetailWidth - kLabelWidth - 1))};
    if ((*r)["row"] == "peer") {
      rows.push_back(label_value("state", r->value("online", false) ? "online" : "unreachable"));
      if (!r->value("sync_note", "").empty())
        rows.push_back(label_value("syncing", r->value("sync_note", "")));
      rows.push_back(label_value("transport",
                                 std::string(r->value("transport", "daemon") == "ssh"
                                                 ? "rsync over SSH"
                                                 : "rsync daemon (LAN/VPN)") +
                                     (r->value("ssh_offered", false) ? "" : "; no SSH offered")));
      rows.push_back(label_value("last sync", r->value("last_sync", "").substr(0, 19)));
      rows.push_back(label_value("queued calls", std::to_string(r->value("outbox", 0))));
      if (!r->value("last_error", "").empty())
        rows.push_back(label_value("last error",
                                   paragraph(r->value("last_error", "")) | color(Color::Red)));
    } else {
      rows.push_back(label_value("request", r->value("direction", "") == "in" ? "incoming"
                                                                              : "outgoing"));
      rows.push_back(label_value("since", r->value("created", "").substr(0, 19)));
      if (!r->value("message", "").empty())
        rows.push_back(label_value("note", r->value("message", "")));
      if (r->value("direction", "") == "in")
        rows.push_back(paragraph("Check that this fingerprint matches 'reifs fingerprint' on " +
                                 r->value("name", "") + " before accepting.") | dim);
    }
    return vbox(rows);
  }

  void start_pairing() {
    ctx_.dialogs->ask_text("Address of the server to pair with (HOST[:PORT])", "",
                           [this](const std::string& address) {
      if (address.empty()) return;
      ctx_.send("/api/peers/probe", {{"address", address}}, [this, address](json info) {
        std::string fp = info.value("fingerprint", "");
        ctx_.dialogs->ask_choice(
            "Server '" + info.value("name", "") + "' at " + address +
                ".\nIts certificate fingerprint is\n" + fp +
                "\nCompare it with 'reifs fingerprint' on that server.",
            {"Send a pairing request", "Cancel"}, [this, address, fp](int i) {
              if (i != 0) return;
              ctx_.send("/api/peers/pair", {{"address", address}, {"fingerprint", fp}},
                        [this](json) {
                          ctx_.notify("pairing requested; accept it on the other server");
                          refresh();
                        });
            });
      });
    });
  }

  void ssh_key_dialog() {
    std::string key = self_.value("ssh_public_key", "");
    std::string state = self_.value("ssh_enabled", false)
                            ? "This server offers SSH to its peers."
                            : "This server does not offer SSH ([ssh] enabled = false).";
    ctx_.dialogs->ask_choice(
        state + "\nIts own key (used to pull from peers over SSH):\n" + key +
            "\n\nInstall the paired servers' keys into this machine's authorized_keys "
            "(read-only rsync with rrsync)?",
        {"Close", "Install the peers' keys"}, [this](int i) {
          if (i != 1) return;
          ctx_.send("/api/peers/authorize", {{"apply", true}}, [this](json out) {
            std::string msg = "added " + std::to_string(out.value("added", 0)) + " key(s) to " +
                              out.value("file", "");
            if (!out.value("restricted", false)) msg += " (rrsync not found: NOT restricted)";
            if (!out["missing"].empty()) msg += "; no key yet for " + join(out["missing"]);
            ctx_.notify(msg);
          });
        });
  }

  bool on_event(const Event& e) {
    if (key(e, "r")) return refresh(), true;
    if (key(e, "p")) return start_pairing(), true;
    if (key(e, "k")) return ssh_key_dialog(), true;
    if (key(e, "s")) {
      ctx_.send("/api/peers/sync", {}, [this](json) { ctx_.notify("syncing"); });
      return true;
    }
    const json* r = current();
    if (!r) return false;
    json row = *r;
    if ((key(e, "a") || key(e, "x")) && row["row"] == "request") {
      bool accept = key(e, "a");
      if (accept && row.value("direction", "") != "in") {
        ctx_.error("that request is waiting for the other server to accept");
        return true;
      }
      std::string q = accept ? "Accept pairing with '" + row.value("name", "") + "'?\n"
                                   "Fingerprint " + row.value("fingerprint", "")
                             : "Reject this pairing request?";
      ctx_.dialogs->confirm(q, [this, row, accept] {
        ctx_.send("/api/peers/requests/" + std::to_string(row.value("id", 0)) +
                      (accept ? "/accept" : "/reject"),
                  {}, [this, row, accept](json) {
                    ctx_.notify(accept ? "paired with " + row.value("name", "")
                                       : "request rejected");
                    refresh();
                  });
      });
      return true;
    }
    if (key(e, "t") && row["row"] == "peer") {
      std::string next = row.value("transport", "daemon") == "ssh" ? "daemon" : "ssh";
      ctx_.send("/api/peers/" + url_encode(row.value("id", "")) + "/transport",
                {{"transport", next}}, [this, row, next](json) {
                  ctx_.notify("files from " + row.value("name", "") + " now come over " +
                              (next == "ssh" ? "SSH" : "its rsync daemon"));
                  refresh();
                });
      return true;
    }
    if (key(e, "d") && row["row"] == "peer") {
      ctx_.dialogs->confirm("Unpair from '" + row.value("name", "") +
                                "'? Its nodes stay listed but stop updating.",
                            [this, row] {
                              ctx_.send("/api/peers/" + url_encode(row.value("id", "")) +
                                            "/unpair",
                                        {}, [this](json) {
                                          ctx_.notify("unpaired");
                                          refresh();
                                        });
                            });
      return true;
    }
    return false;
  }

  Context& ctx_;
  json self_ = json::object();
  json rows_ = json::array();
  int polls_ = 0;
  std::vector<std::string> labels_;
  int selected_ = 0;
  Component menu_, root_;
};

// --- Marked for deletion -----------------------------------------------------------

class MarkedView : public View {
 public:
  explicit MarkedView(Context& ctx) : ctx_(ctx) {
    menu_ = Menu(&labels_, &selected_, MenuOption::Vertical());
    root_ = Renderer(menu_, [this] {
      auto list = list_panel(std::to_string(total_) + " cop(ies) marked for deletion, " +
                                 human_size(total_size_) +
                                 (total_ > static_cast<int>(items_.size())
                                      ? " (first " + std::to_string(items_.size()) + " shown)"
                                      : ""),
                             menu_, labels_.size(), "nothing is marked for deletion");
      Element detail = text("");
      if (selected_ >= 0 && selected_ < static_cast<int>(items_.size())) {
        const json& m = items_[selected_];
        detail = vbox({hard_wrap(m.value("path", ""), kDetailWidth - 1) | bold, separator(),
                       label_value("node", m.value("node", "")),
                       label_value("size", human_size(m.value("size", std::int64_t{0}))),
                       label_path("physical", m.value("physical", ""))});
      }
      return vbox({paragraph(" The server never deletes files. Delete these yourself; the next "
                             "scan clears their marks. Or keep them with u / U.") |
                       dim,
                   with_detail(list, detail) | flex});
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Marked"; }
  Component component() override { return root_; }
  std::string help() const override { return "u: keep (unmark) this copy  U: unmark all  r: refresh"; }

  void refresh() override {
    ctx_.get("/api/marked?limit=1000", [this](json out) {
      items_ = out["marked"];
      total_ = out.value("total", static_cast<int>(items_.size()));
      total_size_ = out.value("total_size", std::int64_t{0});
      labels_.clear();
      for (const auto& m : items_)
        labels_.push_back(fit(m.value("node", ""), 12) + fit(m.value("path", ""), 50) + " " +
                          human_size(m.value("size", std::int64_t{0})));
      selected_ = std::min(selected_, std::max(0, static_cast<int>(items_.size()) - 1));
    }, "marked");
  }

 private:
  bool on_event(const Event& e) {
    if (key(e, "r")) return refresh(), true;
    if (key(e, "U")) {
      ctx_.dialogs->confirm("Remove every deletion mark?", [this] {
        ctx_.send("/api/unmark", {{"all", true}}, [this](json out) {
          ctx_.notify("unmarked " + std::to_string(out.value("unmarked", 0)) + " cop(ies)");
          refresh();
        });
      });
      return true;
    }
    if (key(e, "u") && selected_ >= 0 && selected_ < static_cast<int>(items_.size())) {
      std::string path = items_[selected_].value("path", ""), node = items_[selected_].value("node", "");
      ctx_.send("/api/unmark", {{"paths", {path}}, {"node", node}}, [this, path, node](json) {
        ctx_.notify("kept " + path + " on " + node);
        refresh();
      });
      return true;
    }
    return false;
  }

  int total_ = 0;
  std::int64_t total_size_ = 0;
  Context& ctx_;
  json items_ = json::array();
  std::vector<std::string> labels_;
  int selected_ = 0;
  Component menu_, root_;
};


// --- Schedules ----------------------------------------------------------------------

// A number from a reply that may hold null (value() with a number default throws on a null).
double jdouble(const json& j, const char* key, double fallback = 0.0) {
  auto it = j.find(key);
  return it != j.end() && it->is_number() ? it->get<double>() : fallback;
}

std::string when_text(double epoch) {
  if (epoch <= 0) return "";
  std::time_t t = static_cast<std::time_t>(epoch);
  char buf[32];
  std::tm tm{};
#ifdef _WIN32
  localtime_s(&tm, &t);
#else
  localtime_r(&t, &tm);
#endif
  std::strftime(buf, sizeof buf, "%Y-%m-%d %H:%M", &tm);
  return buf;
}

std::string due_text(double next_due) {
  double secs = next_due - static_cast<double>(std::time(nullptr));
  if (secs <= 0) return "now";
  if (secs < 5400) return "in " + std::to_string(static_cast<int>(secs / 60) + 1) + " min";
  if (secs < 172800) return "in " + std::to_string(static_cast<int>(secs / 3600)) + " h";
  return "in " + std::to_string(static_cast<int>(secs / 86400)) + " days";
}

// --- Applications -------------------------------------------------------------------

class AppsView : public View {
 public:
  explicit AppsView(Context& ctx) : ctx_(ctx) {
    menu_ = Menu(&labels_, &selected_, MenuOption::Vertical());
    root_ = Renderer(menu_, [this] {
      return vbox({paragraph(" Applications use rei-fs for their files through the API. A storage "
                             "app gets a directory on a node to fill. The instances of an app "
                             "registered on several nodes keep their directory the same "
                             "everywhere (it is made synchronous).") | dim,
                   with_detail(list_panel(std::to_string(items_.size()) + " registration(s)",
                                          menu_, labels_.size(), "none yet: n registers one"),
                               detail()) | flex});
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Apps"; }
  Component component() override { return root_; }
  std::string help() const override { return "n: register an app  t: new token  x: unregister  r: refresh"; }

  void refresh() override {
    ctx_.get("/api/apps", [this](json out) {
      items_ = out["apps"];
      labels_.clear();
      for (const auto& a : items_)
        labels_.push_back(fit(a.value("name", ""), 22) + fit(a.value("mode", ""), 10) +
                          fit(a.value("node", ""), 16) + fit(a.value("dir", ""), 26) +
                          (a.value("online", false) ? "" : "offline"));
      selected_ = std::min(selected_, std::max(0, static_cast<int>(labels_.size()) - 1));
    }, "apps");
    ctx_.get("/api/nodes", [this](json out) { nodes_ = out["nodes"]; }, "apps-nodes");
  }

 private:
  Element detail() {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return text("");
    const json& a = items_[selected_];
    Elements rows = {text(a.value("name", "")) | bold, separator(),
                     label_value("mode", a.value("mode", "")),
                     label_value("node", a.value("node", "") + " (server " + a.value("server", "") + ")"),
                     label_value("directory", a.value("dir", "")),
                     label_value("token", a.value("has_token", false) ? "issued here"
                                                                      : "issued on its own server")};
    int same = 0;
    for (const auto& o : items_)
      if (o.value("name", "") == a.value("name", "")) ++same;
    if (a.value("mode", "") == "instance") rows.push_back(label_value("instances", std::to_string(same)));
    rows.push_back(text(""));
    rows.push_back(text("the app presents its token as 'Authorization: Bearer ...'") | dim);
    rows.push_back(text("on /api/app/list, read, write, delete, sync, info") | dim);
    return vbox(rows);
  }

  bool on_event(const Event& e) {
    if (key(e, "r")) return refresh(), true;
    if (key(e, "n")) {
      register_app();
      return true;
    }
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return false;
    json a = items_[selected_];
    json who = {{"name", a.value("name", "")}, {"node", a.value("node", "")}};
    if (!a.value("mine", false)) who["server"] = server_of(a.value("node", ""));
    if (key(e, "t")) {
      ctx_.dialogs->confirm("Make a new token for " + a.value("name", "") +
                                "? The old one stops working.", [this, who] {
        ctx_.send("/api/apps/token", who, [this](json out) {
          ctx_.dialogs->info("New token (shown once)", out.value("token", ""));
        });
      });
      return true;
    }
    if (key(e, "x")) {
      ctx_.dialogs->confirm("Unregister " + a.value("name", "") + " on " + a.value("node", "") +
                                "? Its directory and files stay as they are.", [this, who] {
        ctx_.send("/api/apps/remove", who, [this](json) { refresh(); });
      });
      return true;
    }
    return false;
  }

  std::string server_of(const std::string& node) const {
    for (const auto& n : nodes_)
      if (n.value("name", "") == node) return n.value("owner_id", "");
    return "";
  }

  void register_app() {
    ctx_.dialogs->ask_text("Name of the application (letters, digits . _ -):", "",
                           [this](const std::string& name) {
      if (name.empty()) return;
      ctx_.dialogs->ask_choice(
          "How does " + name + " use rei-fs?",
          {"storage: a directory on a node to fill with whatever it wants",
           "instance: its files are kept the same on every node it is registered on"},
          [this, name](int m) {
            std::string mode = m == 0 ? "storage" : "instance";
            std::vector<std::string> labels = {"any node (one is chosen)"};
            std::vector<json> choices = {json(nullptr)};
            for (const auto& n : nodes_) {
              if (!n.value("online", false)) continue;
              labels.push_back(n.value("name", "") + "  (" + n.value("owner", "") + ")");
              choices.push_back(n);
            }
            ctx_.dialogs->ask_choice(
                "On which node?", labels, [this, name, mode, choices](int i) {
                  json body = {{"name", name}, {"mode", mode}};
                  if (!choices[i].is_null()) {
                    body["node"] = choices[i].value("name", "");
                    // A node of another server is registered there.
                    if (choices[i].value("owner", "") != "self")
                      body["server"] = choices[i].value("owner_id", "");
                  }
                  ctx_.send("/api/apps", body, [this, name](json out) {
                    ctx_.dialogs->info(
                        "Registered " + name,
                        "node: " + out.value("node", "") + "\ndirectory: " + out.value("dir", "") +
                            (out["path"].is_string() ? "\non disk: " + out.value("path", "") : "") +
                            "\n\ntoken (shown once, keep it):\n" + out.value("token", ""));
                    refresh();
                  });
                });
          });
    });
  }

  Context& ctx_;
  json items_ = json::array();
  json nodes_ = json::array();
  std::vector<std::string> labels_;
  int selected_ = 0;
  Component menu_, root_;
};

class SchedulesView : public View {
 public:
  explicit SchedulesView(Context& ctx) : ctx_(ctx) {
    menu_ = Menu(&labels_, &selected_, MenuOption::Vertical());
    root_ = Renderer(menu_, [this] {
      return vbox({paragraph(" A schedule keeps a directory of one node copied to another at regular "
                             "intervals. Missing files are copied, older copies are replaced by "
                             "newer ones, nothing is ever deleted. A node that is asleep when it is "
                             "due is caught up when it is back.") | dim,
                   with_detail(list_panel(std::to_string(items_.size()) + " schedule(s)" + note_,
                                          menu_, labels_.size(),
                                          "none yet: n adds one (or S on a directory in the Tree)"),
                               detail()) | flex});
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Schedules"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "n: new  s: sync now  e: enable/disable  i: interval  p: directory  u: replace older on/off  "
           "x: delete  r: refresh";
  }

  void refresh() override {
    ctx_.get("/api/schedules?everywhere=1", [this](json out) {
      std::string keep = current_key();
      items_ = out["schedules"];
      note_ = unreachable_note(out);
      labels_.clear();
      for (const auto& r : items_) {
        std::string state = !r.value("enabled", false) ? "off"
                            : !r.value("waiting", "").empty() ? "WAITING"
                                                             : "on";
        labels_.push_back(fit("#" + std::to_string(r.value("id", 0)), 5) +
                          fit(r.value("name", ""), 26) + fit(r.value("every", ""), 5) +
                          fit(state, 9) + fit(due_text(jdouble(r, "next_due")), 11) +
                          r.value("note", "") + where_suffix(ctx_, r));
      }
      selected_ = reselect(items_, keep, key_of, selected_);
    }, "schedules");
  }
  void on_poll() override {
    if (++polls_ % 5 == 0) refresh();
  }

 private:
  static std::string key_of(const json& r) { return r.value("server", "") + r["id"].dump(); }
  std::string current_key() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return "";
    return key_of(items_[selected_]);
  }

  Element detail() {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return text("");
    const json& r = items_[selected_];
    Elements rows = {paragraph(r.value("name", "")) | bold, separator(),
                     label_value("directory", r.value("path", "")),
                     label_value("from", r.value("from", "")),
                     label_value("to", r.value("to", "")),
                     label_value("every", r.value("every", "")),
                     label_value("replaces older", r.value("update_changed", true) ? "yes" : "no"),
                     label_value("state", r.value("enabled", false) ? "on" : "off"),
                     label_value("next run", r.value("enabled", false)
                                                 ? due_text(jdouble(r, "next_due"))
                                                 : "(off)"),
                     label_value("last run", when_text(jdouble(r, "last_run")))};
    if (r["last_job"].is_number())
      rows.push_back(label_value("last job", "#" + std::to_string(r["last_job"].get<long>()) + " " +
                                                 r.value("last_job_status", "")));
    if (!r.value("waiting", "").empty())
      rows.push_back(paragraph("waiting: " + r.value("waiting", "")) | color(Color::Yellow) | bold);
    else if (!r.value("note", "").empty())
      rows.push_back(paragraph(r.value("note", "")) | dim);
    return vbox(rows);
  }

  bool on_event(const Event& e) {
    if (key(e, "r")) return refresh(), true;
    if (key(e, "n")) {
      schedule_dialog(ctx_, "/", [this] { refresh(); });
      return true;
    }
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return false;
    json r = items_[selected_];
    std::string base = "/api/schedules/" + std::to_string(r.value("id", 0));
    if (key(e, "s")) {
      ctx_.send(base + "/run", on_server(json::object(), r), [this](json out) {
        ctx_.notify(out.value("note", "done"));
        refresh();
      });
      return true;
    }
    if (key(e, "e")) {
      ctx_.send(base, on_server({{"enabled", !r.value("enabled", false)}}, r),
                [this](json) { refresh(); });
      return true;
    }
    if (key(e, "u")) {
      ctx_.send(base, on_server({{"update_changed", !r.value("update_changed", true)}}, r),
                [this](json) { refresh(); });
      return true;
    }
    if (key(e, "i")) {
      std::vector<std::string> opts;
      for (const auto& [label, _] : interval_choices()) opts.push_back(label);
      ctx_.dialogs->ask_choice("How often?", opts, [this, r, base](int i) {
        ctx_.send(base, on_server({{"every", interval_choices()[i].second}}, r),
                  [this](json) { refresh(); });
      });
      return true;
    }
    if (key(e, "p")) {   // the directory it keeps in sync (after it was renamed outside rei-fs)
      ctx_.dialogs->ask_text("Directory to keep synced:", r.value("path", "/"),
                             [this, r, base](const std::string& value) {
        if (value.empty()) return;
        ctx_.send(base, on_server({{"path", value}}, r), [this](json) { refresh(); });
      });
      return true;
    }
    if (key(e, "x")) {
      ctx_.dialogs->confirm("Delete the schedule '" + r.value("name", "") +
                                "'? (Files already copied stay.)", [this, r, base] {
        ctx_.send(base + "/delete", on_server(json::object(), r), [this](json) { refresh(); });
      });
      return true;
    }
    return false;
  }

  Context& ctx_;
  json items_ = json::array();
  std::string note_;
  int polls_ = 0;
  std::vector<std::string> labels_;
  int selected_ = 0;
  Component menu_, root_;
};

}  // namespace

std::vector<std::shared_ptr<View>> make_views(Context& ctx) {
  return {
      std::make_shared<OverviewView>(ctx),  std::make_shared<TreeView>(ctx),
      std::make_shared<SearchView>(ctx),    std::make_shared<DecisionsView>(ctx),
      std::make_shared<WantedView>(ctx),    std::make_shared<JobsView>(ctx),
      std::make_shared<NodesView>(ctx),     std::make_shared<MarkedView>(ctx),
      std::make_shared<ServersView>(ctx),   std::make_shared<SchedulesView>(ctx),
      std::make_shared<AppsView>(ctx),
  };
}

}  // namespace reifs
