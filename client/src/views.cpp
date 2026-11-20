// The client's tabs: Overview, Tree, Search, Decisions, Wanted, Jobs, Nodes,
// Marked. Each view keeps its own data and talks to the server through the
// shared Context.
#include <algorithm>
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

Element copies_badge(int good) {
  auto t = text(std::to_string(good) + "x");
  if (good >= 2) return t | color(Color::Green);
  if (good == 1) return t | color(Color::Yellow);
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
         fit(human_size(f.value("size", 0)), 10) + " " +
         fit(std::to_string(f.value("good_copies", 0)) + "x" + flags, 16);
}

// Detail panel for /api/file output.
Element file_details(const json& f) {
  if (f.is_null() || f.empty()) return text("");
  Elements rows = {
      hard_wrap(f.value("path", ""), kDetailWidth - 1) | bold,
      separator(),
      label_value("size", human_size(f.value("size", 0))),
      label_value("modified", format_time(f.value("mtime", 0))),
      label_value("good copies", hbox({copies_badge(f.value("good_copies", 0)), text(" / 2")})),
  };
  if (!f.value("tags", json::array()).empty())
    rows.push_back(label_value("tags", join(f["tags"])));
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
                                         human_size(c.value("size", 0)) + "  " +
                                         c.value("hash", "")) | dim}));
  }
  return vbox(rows);
}

Element list_panel(const std::string& heading, Component menu, size_t count,
                   const std::string& empty_text) {
  Element body = count == 0 ? text(empty_text) | dim | center
                            : menu->Render() | vscroll_indicator | frame;
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
    ctx.attempt([&] {
      auto out = ctx.api->post("/api/tags", {{"action", add ? "add" : "remove"},
                                             {"tag", tag},
                                             {"paths", paths},
                                             {"recursive", recursive}});
      ctx.notify(std::string(add ? "tagged " : "untagged ") +
                 std::to_string(out.value("changed", 0)) + " file(s)");
    });
    done();
  });
}

// Choose nodes and create a copy or move job.
void transfer_paths(Context& ctx, std::vector<std::string> paths, const std::string& tag,
                    bool move) {
  if (paths.empty() && tag.empty()) return;
  auto nodes = ctx.node_names();
  if (nodes.empty()) return;
  auto create = [&ctx, paths, tag, move](const std::string& from, const std::string& to) {
    ctx.attempt([&] {
      json body = {{"kind", move ? "move" : "copy"}, {"paths", paths}, {"to", to}};
      if (!tag.empty()) body["tag"] = tag;
      if (!from.empty()) body["from"] = from;
      auto out = ctx.api->post("/api/jobs", body);
      if (out["job_id"].is_null()) {
        ctx.notify("nothing to do (already there, or not on the source)");
      } else {
        ctx.notify("job #" + std::to_string(out.value("job_id", 0)) + " created with " +
                   std::to_string(out.value("items", 0)) + " item(s); it runs in the background");
      }
    });
  };
  std::string what = !tag.empty() ? "files tagged '" + tag + "'"
                     : paths.size() == 1 ? paths[0]
                                         : std::to_string(paths.size()) + " items";
  if (!move) {
    ctx.dialogs->ask_choice("Copy " + what + " to which node?", nodes,
                            [create, nodes](int i) { create("", strip_offline(nodes[i])); });
    return;
  }
  ctx.dialogs->ask_choice(
      "Move " + what + " FROM which node? (its copies get marked for deletion)", nodes,
      [&ctx, create, nodes, what](int i) {
        std::string from = strip_offline(nodes[i]);
        ctx.dialogs->ask_choice("Move " + what + " from " + from + " TO which node?", nodes,
                                [create, nodes, from](int j) {
                                  create(from, strip_offline(nodes[j]));
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

class OverviewView : public View {
 public:
  explicit OverviewView(Context& ctx) : ctx_(ctx) {
    root_ = Renderer([this] { return render(); });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Overview"; }
  Component component() override { return root_; }
  void refresh() override {}
  std::string help() const override { return "s: scan all nodes  j: run jobs  x: stop current work"; }

 private:
  bool on_event(const Event& e) {
    if (key(e, "s")) {
      if (ctx_.attempt([&] { ctx_.api->post("/api/scan"); })) ctx_.notify("scan queued");
      return true;
    }
    if (key(e, "j")) {
      if (ctx_.attempt([&] { ctx_.api->post("/api/jobs/run"); })) ctx_.notify("job run queued");
      return true;
    }
    if (key(e, "x")) {
      ctx_.dialogs->confirm("Stop the current scan or job run? (progress is kept)", [this] {
        if (ctx_.attempt([&] { ctx_.api->post("/api/stop"); })) ctx_.notify("stop requested");
      });
      return true;
    }
    return false;
  }

  Element render() {
    json s = ctx_.status();
    if (s.empty()) return text("waiting for the server...") | center;
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
        line("files", num("files") + "  (" + human_size(s.value("bytes", 0)) + ")"),
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
        separator(),
        line("activity", act.value("state", "")),
    };
    if (!act.value("detail", "").empty()) right.push_back(line("", act.value("detail", "")));
    if (!act.value("last_result", "").empty())
      right.push_back(hbox({text(fit("last result", 26)), paragraph(act.value("last_result", ""))}));
    if (!act.value("last_error", "").empty())
      right.push_back(hbox({text(fit("last error", 26)),
                            paragraph(act.value("last_error", "")) | color(Color::Red)}));
    return hbox({vbox(left) | flex | border, vbox(right) | flex | border});
  }

  Context& ctx_;
  Component root_;
};

// --- Tree ---------------------------------------------------------------------------

class TreeView : public View {
 public:
  explicit TreeView(Context& ctx) : ctx_(ctx) {
    MenuOption opt = MenuOption::Vertical();
    opt.on_change = [this] { load_detail(); };
    opt.on_enter = [this] { enter(); };
    menu_ = Menu(&labels_, &selected_, opt);
    root_ = Renderer(menu_, [this] {
      return with_detail(list_panel(path_, menu_, labels_.size(), "(empty)"), detail_);
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Tree"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "Enter/→: open  ←/Backspace: up  t/T: tag/untag  c: copy  m: move  r: refresh";
  }

  void refresh() override {
    std::string keep = current_name();
    bool ok = ctx_.attempt([&] {
      try {
        entries_ = ctx_.api->get("/api/tree?path=" + url_encode(path_))["entries"];
      } catch (const ApiError& e) {
        if (e.status != 404 || path_ == "/") throw;
        path_ = "/";  // the directory disappeared
        entries_ = ctx_.api->get("/api/tree?path=/")["entries"];
      }
    });
    if (!ok) entries_ = json::array();
    labels_.clear();
    for (const auto& e : entries_) {
      if (e.value("dir", false))
        labels_.push_back(fit("▸ " + e.value("name", "") + "/", 42) + " " +
                          std::to_string(e.value("files", 0)) + " files");
      else
        labels_.push_back("  " + file_label(e, 40));
    }
    selected_ = reselect(entries_, keep, [](const json& e) { return e.value("name", ""); },
                         selected_);
    load_detail();
  }

 private:
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

  void load_detail() {
    const json* e = current();
    if (!e) {
      detail_ = text("");
      return;
    }
    if (e->value("dir", false)) {
      detail_ = vbox({hard_wrap(child_path(e->value("name", "")) + "/", kDetailWidth - 1) | bold,
                      separator(),
                      text(std::to_string(e->value("files", 0)) + " file(s) below"),
                      text(""), text("t: tag every file below  c/m: copy/move it all") | dim});
      return;
    }
    json f;
    if (ctx_.attempt([&] { f = ctx_.api->get("/api/file?path=" + url_encode(child_path(e->value("name", "")))); }))
      detail_ = file_details(f);
  }

  void enter() {
    const json* e = current();
    if (!e || !e->value("dir", false)) return;
    path_ = child_path(e->value("name", ""));
    selected_ = 0;
    refresh();
  }

  void up() {
    if (path_ == "/") return;
    auto pos = path_.rfind('/');
    std::string child = path_.substr(pos + 1);
    path_ = pos == 0 ? "/" : path_.substr(0, pos);
    refresh();
    selected_ = reselect(entries_, child, [](const json& e) { return e.value("name", ""); }, 0);
    load_detail();
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
    return false;
  }

  Context& ctx_;
  std::string path_ = "/";
  json entries_ = json::array();
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
    query_input_ = Input(&query_, "name glob, e.g. *.jpg (or a path glob with /)", opt);
    tag_input_ = Input(&tag_, "tag", opt);
    auto issues = Checkbox("non-portable", &issues_);
    auto under = Checkbox("under-replicated", &under_);
    auto button = Button("Search", [this] { search(); }, ButtonOption::Ascii());
    MenuOption mopt = MenuOption::Vertical();
    mopt.on_change = [this] { load_detail(); };
    menu_ = Menu(&labels_, &selected_, mopt);
    auto form = Container::Horizontal({query_input_, tag_input_, issues, under, button});
    auto layout = Container::Vertical({form, menu_});
    root_ = Renderer(layout, [=, this] {
      auto form_el = hbox({
          query_input_->Render() | border | flex,
          tag_input_->Render() | border | size(WIDTH, EQUAL, 20),
          vbox({issues->Render(), under->Render()}),
          button->Render() | vcenter,
      });
      std::string heading = results_.empty() ? "results"
                                              : std::to_string(total_) + " result(s)" +
                                                    (truncated_ ? " (first 1000 shown)" : "");
      return vbox({form_el, text(" tags: " + tags_line_) | dim,
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
    return "Enter: search  Esc/↓: results  t/T: tag/untag  c/m: copy/move  C/M: copy/move all";
  }

  void refresh() override {
    ctx_.attempt([&] {
      tags_line_.clear();
      json out = ctx_.api->get("/api/tags");  // keep the temporary alive for the loop
      for (const auto& t : out["tags"])
        tags_line_ += t.value("tag", "") + "(" + std::to_string(t.value("files", 0)) + ") ";
      if (tags_line_.empty()) tags_line_ = "(none)";
    });
    if (searched_) search();
  }

 private:
  void search() {
    std::string q = "/api/find?q=" + url_encode(query_) + "&tag=" + url_encode(tag_);
    if (issues_) q += "&issues=1";
    if (under_) q += "&under=1";
    json out;
    if (!ctx_.attempt([&] { out = ctx_.api->get(q); })) return;
    searched_ = true;
    results_ = out["files"];
    total_ = out.value("total", 0);
    truncated_ = out.value("truncated", false);
    labels_.clear();
    for (const auto& f : results_) {
      json shown = f;
      shown["name"] = f.value("path", "");
      labels_.push_back(file_label(shown, 50));
    }
    selected_ = std::min(selected_, std::max(0, static_cast<int>(labels_.size()) - 1));
    load_detail();
    if (!results_.empty()) menu_->TakeFocus();
  }

  void load_detail() {
    if (selected_ < 0 || selected_ >= static_cast<int>(results_.size())) {
      detail_ = text("");
      return;
    }
    json f;
    if (ctx_.attempt([&] {
          f = ctx_.api->get("/api/file?path=" + url_encode(results_[selected_].value("path", "")));
        }))
      detail_ = file_details(f);
  }

  std::vector<std::string> all_paths() const {
    std::vector<std::string> out;
    for (const auto& f : results_) out.push_back(f.value("path", ""));
    return out;
  }

  bool on_event(const Event& e) {
    if (e == Event::Escape && typing()) {
      // Leave the text fields so the global keys (tabs, q) work again.
      if (!results_.empty()) menu_->TakeFocus();  // F1-F8 work in any case
      return true;
    }
    if (!menu_->Focused() || results_.empty()) return false;
    std::string path = results_[selected_].value("path", "");
    if (key(e, "t") || key(e, "T")) {
      tag_paths(ctx_, {path}, false, key(e, "t"), [this] { search(); });
      return true;
    }
    if (key(e, "c") || key(e, "m")) {
      transfer_paths(ctx_, {path}, "", key(e, "m"));
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
      search();
      return true;
    }
    return false;
  }

  Context& ctx_;
  std::string query_, tag_, tags_line_;
  bool issues_ = false, under_ = false, searched_ = false, truncated_ = false;
  int total_ = 0;
  json results_ = json::array();
  std::vector<std::string> labels_;
  int selected_ = 0;
  Element detail_ = text("");
  Component query_input_, tag_input_, menu_, root_;
};

// --- Decisions ----------------------------------------------------------------------

class DecisionsView : public View {
 public:
  explicit DecisionsView(Context& ctx) : ctx_(ctx) {
    MenuOption opt = MenuOption::Vertical();
    opt.on_enter = [this] { decide_current(); };
    menu_ = Menu(&labels_, &selected_, opt);
    root_ = Renderer(menu_, [this] {
      return with_detail(list_panel(std::to_string(items_.size()) + " decision(s) need attention",
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
    std::string keep = current_id();
    if (!ctx_.attempt([&] { items_ = ctx_.api->get("/api/decisions")["decisions"]; }))
      items_ = json::array();
    labels_.clear();
    for (const auto& d : items_)
      labels_.push_back(fit("[" + d.value("kind", "") + "]", 12) + d.value("path", "") +
                        (d["node"].is_string() ? "  (" + d.value("node", "") + ")" : ""));
    selected_ = reselect(items_, keep, [](const json& d) { return d["id"].dump(); }, selected_);
  }

 private:
  std::string current_id() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return "";
    return items_[selected_]["id"].dump();
  }

  Element detail() {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return text("");
    const json& d = items_[selected_];
    Elements rows = {hard_wrap(d.value("path", ""), kDetailWidth - 1) | bold, separator(),
                     label_value("kind", d.value("kind", "")),
                     paragraph(d.value("summary", "")), separator(), text("choices") | bold};
    for (const auto& o : d["options"])
      rows.push_back(paragraph("• " + o.value("label", "")));
    return vbox(rows);
  }

  void resolve(int id, const std::string& option) {
    ctx_.attempt([&] {
      auto out = ctx_.api->post("/api/decisions/" + std::to_string(id), {{"option", option}});
      ctx_.notify(out.value("summary", "done"));
    });
  }

  void decide_current() {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return;
    json d = items_[selected_];
    std::vector<std::string> labels;
    for (const auto& o : d["options"]) labels.push_back(o.value("label", ""));
    ctx_.dialogs->ask_choice(d.value("summary", "") + "\n" + d.value("path", ""), labels,
                             [this, d](int i) {
                               resolve(d.value("id", 0), d["options"][i].value("key", ""));
                               refresh();
                             });
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
                int done = 0;
                for (const auto& x : items_) {
                  if (x.value("kind", "") != kind) continue;
                  bool ok = ctx_.attempt([&] {
                    ctx_.api->post("/api/decisions/" + std::to_string(x.value("id", 0)),
                                   {{"option", key}});
                  });
                  done += ok;
                }
                ctx_.notify("applied to " + std::to_string(done) + " decision(s)");
                refresh();
              });
        });
  }

  bool on_event(const Event& e) {
    if (key(e, "r")) {
      refresh();
      return true;
    }
    if (key(e, "a")) {
      decide_all_of_kind();
      return true;
    }
    return false;
  }

  Context& ctx_;
  json items_ = json::array();
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
      return vbox({list_panel(std::to_string(items_.size()) + " proposed job(s), " +
                                  std::to_string(checked_.size()) + " selected",
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
    if (!ctx_.attempt([&] { items_ = ctx_.api->get("/api/wanted")["wanted"]; }))
      items_ = json::array();
    std::set<int> still;
    for (const auto& w : items_)
      if (checked_.count(w.value("id", 0))) still.insert(w.value("id", 0));
    checked_ = still;
    relabel();
    selected_ = std::min(selected_, std::max(0, static_cast<int>(items_.size()) - 1));
  }

 private:
  void relabel() {
    labels_.clear();
    for (const auto& w : items_) {
      bool on = checked_.count(w.value("id", 0)) > 0;
      std::string route = w.value("from", "").empty() ? "→ " + w.value("to", "")
                                                      : w.value("from", "") + " → " + w.value("to", "");
      labels_.push_back(std::string(on ? "[x] " : "[ ] ") + fit(w.value("op", ""), 10) +
                        fit(w.value("path", ""), 50) + " " + fit(route, 24) + " " +
                        w.value("reason", ""));
    }
  }

  std::vector<int> targets() const {
    if (!checked_.empty()) return {checked_.begin(), checked_.end()};
    if (selected_ >= 0 && selected_ < static_cast<int>(items_.size()))
      return {items_[selected_].value("id", 0)};
    return {};
  }

  void act(const std::string& action, bool all) {
    json body = {{"action", action}};
    if (all) {
      body["all"] = true;
    } else {
      auto ids = targets();
      if (ids.empty()) return;
      body["ids"] = ids;
    }
    ctx_.attempt([&] {
      auto out = ctx_.api->post("/api/wanted", body);
      if (action == "approve")
        ctx_.notify(out["job_id"].is_null()
                        ? "nothing approved"
                        : "job #" + std::to_string(out.value("job_id", 0)) + " created with " +
                              std::to_string(out.value("items", 0)) + " item(s)");
      else
        ctx_.notify("rejected " + std::to_string(out.value("rejected", 0)) + " proposal(s)");
    });
    checked_.clear();
    refresh();
  }

  bool on_event(const Event& e) {
    if (key(e, " ")) {
      if (selected_ >= 0 && selected_ < static_cast<int>(items_.size())) {
        int id = items_[selected_].value("id", 0);
        if (!checked_.erase(id)) checked_.insert(id);
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
  std::set<int> checked_;
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
      return with_detail(list_panel(all_ ? "all jobs" : "active jobs", menu_, labels_.size(),
                                    all_ ? "no jobs" : "no active jobs (h: show finished)"),
                         detail_);
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Jobs"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "j: run now  c: cancel  y: retry failed  h: show/hide finished  r: refresh";
  }
  void on_poll() override { refresh(); }

  void refresh() override {
    std::string keep = current_id();
    if (!ctx_.attempt([&] { items_ = ctx_.api->get(all_ ? "/api/jobs?all=1" : "/api/jobs")["jobs"]; }))
      items_ = json::array();
    labels_.clear();
    for (const auto& j : items_)
      labels_.push_back(fit("#" + std::to_string(j.value("id", 0)), 6) +
                        fit(j.value("status", ""), 10) +
                        fit(std::to_string(j.value("done", 0)) + "/" +
                                std::to_string(j.value("total", 0)), 12) +
                        j.value("description", ""));
    selected_ = reselect(items_, keep, [](const json& j) { return j["id"].dump(); }, selected_);
    load_detail();
  }

 private:
  std::string current_id() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return "";
    return items_[selected_]["id"].dump();
  }

  void load_detail() {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) {
      detail_ = text("");
      return;
    }
    json j;
    if (!ctx_.attempt([&] { j = ctx_.api->get("/api/jobs/" + current_id()); })) return;
    Elements rows = {paragraph(j.value("description", "")) | bold, separator(),
                     label_value("status", j.value("status", "")),
                     label_value("created", j.value("created", "").substr(0, 19))};
    std::string counts;
    for (auto& [k, v] : j["counts"].items()) counts += k + " " + v.dump() + "  ";
    rows.push_back(label_value("items", counts));
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
      if (state == "failed" || state == "conflict")
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
      if (ctx_.attempt([&] { ctx_.api->post("/api/jobs/run"); })) ctx_.notify("job run queued");
      return true;
    }
    std::string id = current_id();
    if (id.empty()) return false;
    if (key(e, "c")) {
      ctx_.dialogs->confirm("Cancel job #" + id + "? Items already done stay done.", [this, id] {
        if (ctx_.attempt([&] { ctx_.api->post("/api/jobs/" + id + "/cancel"); }))
          ctx_.notify("job #" + id + " cancelled");
        refresh();
      });
      return true;
    }
    if (key(e, "y")) {
      ctx_.attempt([&] {
        auto out = ctx_.api->post("/api/jobs/" + id + "/retry");
        ctx_.notify("re-queued " + std::to_string(out.value("items", 0)) + " failed item(s)");
      });
      refresh();
      return true;
    }
    return false;
  }

  Context& ctx_;
  bool all_ = false;
  json items_ = json::array();
  std::vector<std::string> labels_;
  int selected_ = 0;
  Element detail_ = text("");
  Component menu_, root_;
};

// --- Nodes ---------------------------------------------------------------------------

class NodesView : public View {
 public:
  explicit NodesView(Context& ctx) : ctx_(ctx) {
    menu_ = Menu(&labels_, &selected_, MenuOption::Vertical());
    root_ = Renderer(menu_, [this] {
      return with_detail(list_panel("nodes", menu_, labels_.size(), "no nodes"), detail());
    });
    root_ |= CatchEvent([this](Event e) { return on_event(e); });
  }
  std::string title() const override { return "Nodes"; }
  Component component() override { return root_; }
  std::string help() const override {
    return "s: scan node  S: scan all  v: verify (re-hash)  p: priority  t: toggle target  r: refresh";
  }

  void refresh() override {
    std::string keep = current_name();
    if (!ctx_.attempt([&] { items_ = ctx_.api->get("/api/nodes")["nodes"]; }))
      items_ = json::array();
    labels_.clear();
    for (const auto& n : items_)
      labels_.push_back(fit(n.value("name", ""), 16) + fit(n.value("kind", ""), 9) +
                        fit(n.value("online", false) ? "online" : "offline", 9) +
                        fit(std::to_string(n.value("files", 0)) + " files", 14) +
                        human_size(n.value("bytes", 0)));
    selected_ = reselect(items_, keep, [](const json& n) { return n.value("name", ""); },
                         selected_);
  }

 private:
  std::string current_name() const {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return "";
    return items_[selected_].value("name", "");
  }

  Element detail() {
    if (selected_ < 0 || selected_ >= static_cast<int>(items_.size())) return text("");
    const json& n = items_[selected_];
    auto yes = [](bool b) { return std::string(b ? "yes" : "no"); };
    return vbox({
        text(n.value("name", "")) | bold,
        separator(),
        label_value("kind", n.value("kind", "")),
        label_value("state", n.value("online", false) ? "online" : "offline"),
        label_value("owner", n.value("owner", "")),
        label_path("root", n.value("root", "")),
        label_value("files", std::to_string(n.value("files", 0)) + " (" +
                                 human_size(n.value("bytes", 0)) + ")"),
        label_value("case-sensitive", yes(n.value("case_sensitive", false))),
        label_value("Windows names", yes(n.value("restricted_names", false))),
        label_value("priority", std::to_string(n.value("priority", 0))),
        label_value("backup target", yes(n.value("replication_target", false))),
        label_value("last scan", n.value("last_scan", "").substr(0, 19)),
    });
  }

  bool on_event(const Event& e) {
    if (key(e, "r")) return refresh(), true;
    if (key(e, "S")) {
      if (ctx_.attempt([&] { ctx_.api->post("/api/scan"); })) ctx_.notify("scan of all nodes queued");
      return true;
    }
    std::string name = current_name();
    if (name.empty()) return false;
    if (key(e, "s")) {
      if (ctx_.attempt([&] { ctx_.api->post("/api/scan", {{"nodes", {name}}}); }))
        ctx_.notify("scan of " + name + " queued");
      return true;
    }
    if (key(e, "v")) {
      ctx_.dialogs->confirm("Re-hash every file on " + name + "? This reads all its data.",
                            [this, name] {
                              if (ctx_.attempt([&] {
                                    ctx_.api->post("/api/scan", {{"nodes", {name}}, {"verify", true}});
                                  }))
                                ctx_.notify("verify scan of " + name + " queued");
                            });
      return true;
    }
    if (key(e, "p")) {
      std::string current = std::to_string(items_[selected_].value("priority", 0));
      ctx_.dialogs->ask_text("Priority of " + name + " (higher = preferred backup target)",
                             current, [this, name](const std::string& value) {
                               ctx_.attempt([&] {
                                 ctx_.api->post("/api/nodes/" + url_encode(name),
                                                {{"priority", std::stoi(value)}});
                               });
                               refresh();
                             });
      return true;
    }
    if (key(e, "t")) {
      bool target = items_[selected_].value("replication_target", true);
      ctx_.attempt([&] {
        ctx_.api->post("/api/nodes/" + url_encode(name), {{"replication_target", !target}});
      });
      refresh();
      return true;
    }
    return false;
  }

  Context& ctx_;
  json items_ = json::array();
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
      std::int64_t total = 0;
      for (const auto& m : items_) total += m.value("size", 0);
      auto list = list_panel(std::to_string(items_.size()) + " cop(ies) marked for deletion, " +
                                 human_size(total),
                             menu_, labels_.size(), "nothing is marked for deletion");
      Element detail = text("");
      if (selected_ >= 0 && selected_ < static_cast<int>(items_.size())) {
        const json& m = items_[selected_];
        detail = vbox({hard_wrap(m.value("path", ""), kDetailWidth - 1) | bold, separator(),
                       label_value("node", m.value("node", "")),
                       label_value("size", human_size(m.value("size", 0))),
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
    if (!ctx_.attempt([&] { items_ = ctx_.api->get("/api/marked")["marked"]; }))
      items_ = json::array();
    labels_.clear();
    for (const auto& m : items_)
      labels_.push_back(fit(m.value("node", ""), 12) + fit(m.value("path", ""), 50) + " " +
                        human_size(m.value("size", 0)));
    selected_ = std::min(selected_, std::max(0, static_cast<int>(items_.size()) - 1));
  }

 private:
  bool on_event(const Event& e) {
    if (key(e, "r")) return refresh(), true;
    if (key(e, "U")) {
      ctx_.dialogs->confirm("Remove every deletion mark?", [this] {
        ctx_.attempt([&] {
          auto out = ctx_.api->post("/api/unmark", {{"all", true}});
          ctx_.notify("unmarked " + std::to_string(out.value("unmarked", 0)) + " cop(ies)");
        });
        refresh();
      });
      return true;
    }
    if (key(e, "u") && selected_ >= 0 && selected_ < static_cast<int>(items_.size())) {
      const json& m = items_[selected_];
      ctx_.attempt([&] {
        ctx_.api->post("/api/unmark", {{"paths", {m.value("path", "")}}, {"node", m.value("node", "")}});
        ctx_.notify("kept " + m.value("path", "") + " on " + m.value("node", ""));
      });
      refresh();
      return true;
    }
    return false;
  }

  Context& ctx_;
  json items_ = json::array();
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
  };
}

}  // namespace reifs
