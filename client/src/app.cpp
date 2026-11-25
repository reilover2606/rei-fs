#include "app.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <iostream>
#include <thread>

#include <ftxui/component/component_options.hpp>
#include <ftxui/component/event.hpp>
#include <ftxui/component/screen_interactive.hpp>
#include <ftxui/dom/elements.hpp>
#include <ftxui/screen/string.hpp>
#include <ftxui/screen/terminal.hpp>

#include "format.hpp"
#include "profiles.hpp"

using namespace ftxui;

namespace reifs {

// --- dialogs ---------------------------------------------------------------------

Dialogs::Dialogs() : holder_(Container::Vertical({})) {}

Component Dialogs::wrap(Component main) { return main | Modal(holder_, &shown_); }

void Dialogs::open(Component content) {
  holder_->DetachAllChildren();
  holder_->Add(content);
  shown_ = true;
  content->TakeFocus();
}

void Dialogs::finish(std::function<void()> then) {
  shown_ = false;
  if (then) {
    // What a dialog's answer starts may fail on an odd reply: say so instead of crashing.
    auto guarded = [then = std::move(then), this] {
      try {
        then();
      } catch (const std::exception& e) {
        info("Something went wrong", e.what());
      }
    };
    if (auto* screen = ScreenInteractive::Active())
      screen->Post(std::move(guarded));
    else
      guarded();
  }
}

// Multi-line text: one paragraph per line.
static Element lines_of(const std::string& body) {
  Elements lines;
  std::string line;
  for (char c : body + "\n") {
    if (c == '\n') {
      lines.push_back(paragraph(line));
      line.clear();
    } else {
      line += c;
    }
  }
  return vbox(lines);
}

static Component escape_closes(Component c, std::function<void()> close) {
  return CatchEvent(c, [close](Event e) {
    if (e == Event::Escape) {
      close();
      return true;
    }
    return false;
  });
}

void Dialogs::ask_text(const std::string& title, const std::string& initial,
                       std::function<void(const std::string&)> on_ok) {
  text_ = initial;
  cursor_ = static_cast<int>(initial.size());  // edit from the end of the text
  auto ok = [this, on_ok] {
    std::string value = text_;
    finish([on_ok, value] { on_ok(value); });
  };
  InputOption opt;
  opt.multiline = false;
  opt.on_enter = ok;
  opt.cursor_position = &cursor_;
  auto input = Input(&text_, "", opt);
  auto buttons = Container::Horizontal({
      Button("OK", ok, ButtonOption::Ascii()),
      Button("Cancel", [this] { finish(nullptr); }, ButtonOption::Ascii()),
  });
  auto body = Container::Vertical({input, buttons});
  auto view = Renderer(body, [=] {
    return vbox({text(title) | bold, separator(),
                 input->Render() | border | size(WIDTH, GREATER_THAN, 50),
                 buttons->Render() | align_right}) |
           border;
  });
  open(escape_closes(view, [this] { finish(nullptr); }));
}

void Dialogs::ask_choice(const std::string& title, std::vector<std::string> options,
                         std::function<void(int)> on_ok) {
  options_ = std::move(options);
  choice_ = 0;
  MenuOption opt = MenuOption::Vertical();
  opt.on_enter = [this, on_ok] {
    int picked = choice_;
    finish([on_ok, picked] { on_ok(picked); });
  };
  auto menu = Menu(&options_, &choice_, opt);
  auto view = Renderer(menu, [=] {
    return vbox({lines_of(title) | bold, separator(),
                 menu->Render() | frame | size(HEIGHT, LESS_THAN, 15), separator(),
                 text("Enter: choose   Esc: cancel") | dim}) |
           size(WIDTH, LESS_THAN, 110) | border;
  });
  open(escape_closes(view, [this] { finish(nullptr); }));
}

void Dialogs::confirm(const std::string& question, std::function<void()> on_yes) {
  ask_choice(question, {"No", "Yes"}, [on_yes](int i) {
    if (i == 1) on_yes();
  });
}

void Dialogs::info(const std::string& title, const std::string& body) {
  auto ok = Button("OK", [this] { finish(nullptr); }, ButtonOption::Ascii());
  auto view = Renderer(ok, [=] {
    return vbox({text(title) | bold, separator(), lines_of(body), separator(),
                 ok->Render() | align_right}) |
           size(WIDTH, LESS_THAN, 110) | border;
  });
  open(escape_closes(view, [this] { finish(nullptr); }));
}

// --- background executor ---------------------------------------------------------

Executor::Executor() : thread_([this] { loop(); }) {}

Executor::~Executor() { stop(); }

void Executor::submit(const std::string& key, std::function<void()> job,
                      std::function<void()> on_dropped) {
  std::vector<std::function<void()>> dropped;
  {
    std::lock_guard lock(mutex_);
    if (stopping_) return;
    if (!key.empty()) {
      for (auto it = queue_.begin(); it != queue_.end();) {
        if (it->key == key) {
          if (it->on_dropped) dropped.push_back(std::move(it->on_dropped));
          it = queue_.erase(it);
        } else {
          ++it;
        }
      }
    }
    queue_.push_back({key, std::move(job), std::move(on_dropped)});
  }
  wake_.notify_one();
  for (auto& d : dropped) d();
}

void Executor::stop() {
  std::vector<Item> left;
  {
    std::lock_guard lock(mutex_);
    stopping_ = true;
    left.swap(queue_);
  }
  wake_.notify_all();
  if (thread_.joinable()) thread_.join();
  for (auto& item : left)
    if (item.on_dropped) item.on_dropped();
}

void Executor::loop() {
  for (;;) {
    Item item;
    {
      std::unique_lock lock(mutex_);
      wake_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
      if (stopping_) return;
      item = std::move(queue_.front());
      queue_.erase(queue_.begin());
    }
    item.job();
  }
}

// --- context -----------------------------------------------------------------------

void Context::start_workers(Api& main, std::function<void(std::function<void()>)> post) {
  post_ = std::move(post);
  read_api_ = main.spawn();
  write_api_ = main.spawn();
  closing_ = false;
}

void Context::abort_requests() {
  closing_ = true;
  if (read_api_) read_api_->abort();    // a request waiting for a server that does not answer
  if (write_api_) write_api_->abort();  // must not keep the program from quitting
}

void Context::stop_workers() {
  closing_ = true;
  read_exec_.stop();
  write_exec_.stop();
}

void Context::finish(std::uint64_t id) {
  std::lock_guard lock(pending_mutex_);
  pending_.erase(id);
}

int Context::pending() const {
  std::lock_guard lock(pending_mutex_);
  return static_cast<int>(pending_.size());
}

double Context::oldest_pending_seconds() const {
  std::lock_guard lock(pending_mutex_);
  if (pending_.empty()) return 0;
  auto oldest = std::min_element(pending_.begin(), pending_.end(),
                                 [](auto& a, auto& b) { return a.second < b.second; });
  return std::chrono::duration<double>(std::chrono::steady_clock::now() - oldest->second).count();
}

void Context::run(bool write, std::function<json(Api&)> work, std::function<void(json)> done,
                  const std::string& key, std::function<void()> on_error) {
  if (closing_ || !read_api_) return;
  std::uint64_t id;
  {
    std::lock_guard lock(pending_mutex_);
    id = ++next_id_;
    pending_[id] = std::chrono::steady_clock::now();
  }
  Api* api = (write ? write_api_ : read_api_).get();
  auto job = [this, api, id, work = std::move(work), done = std::move(done),
              on_error]() mutable {
    json result;
    std::string failure;
    try {
      result = work(*api);
    } catch (const ApiError& e) {
      failure = e.status == 401 ? "the session expired; reconnecting, try again in a moment"
                                : e.what();
    } catch (const UntrustedCertificate&) {
      failure = "the server certificate changed; reconnect to review it";
    } catch (const std::exception& e) {
      failure = e.what();
    }
    if (closing_) return;
    post_([this, id, result = std::move(result), failure, done = std::move(done), on_error] {
      finish(id);
      if (!failure.empty()) {
        error(failure);
        if (on_error) on_error();
      } else if (done) {
        // A reply the view did not expect (a null where a number was assumed...) must
        // show a message, not take the whole program down.
        try {
          done(result);
        } catch (const std::exception& e) {
          error(std::string("unexpected reply from the server: ") + e.what());
        }
      }
    });
  };
  // A job dropped in favour of a newer one with the same key is simply forgotten.
  (write ? write_exec_ : read_exec_).submit(key, std::move(job), [this, id] { finish(id); });
}

void Context::get(const std::string& path, std::function<void(json)> done,
                  const std::string& key) {
  run(false, [path](Api& a) { return a.get(path); }, std::move(done), key);
}

void Context::send(const std::string& path, json body, std::function<void(json)> done) {
  run(true, [path, body = std::move(body)](Api& a) { return a.post(path, body); },
      std::move(done));
}

json Context::status() const {
  std::lock_guard lock(status_mutex_);
  return status_;
}

void Context::set_status(json s) {
  std::lock_guard lock(status_mutex_);
  status_ = std::move(s);
}

std::string Context::poll_error() const {
  std::lock_guard lock(status_mutex_);
  return poll_error_;
}

void Context::set_poll_error(const std::string& e) {
  std::lock_guard lock(status_mutex_);
  poll_error_ = e;
}

// --- the application -------------------------------------------------------------

namespace {

// FTXUI containers ignore events unless a child is focusable; an empty list
// (or a view without inputs) is not, which would swallow every key. Views are
// wrapped in this so their own and the global key handlers always run.
class AlwaysFocusable : public ComponentBase {
 public:
  explicit AlwaysFocusable(Component child) { Add(std::move(child)); }
  bool Focusable() const override { return true; }
};

struct Notice {
  std::string text;
  bool error = false;
  std::chrono::steady_clock::time_point at;
};

class App {
 public:
  App(std::string host, int port) : screen_(ScreenInteractive::Fullscreen()) {
    profiles_ = Profiles::load();
    host_ = host.empty() ? profiles_.last_host : host;
    port_str_ = std::to_string(port > 0 ? port : profiles_.last_port);
    ctx_.dialogs = &dialogs_;
    ctx_.show_in_tree = [this](const std::string& dir, const std::string& select) {
      if (views_.size() < 2) return;
      views_[1]->navigate(dir, select);   // the Tree is the second tab
      switch_to(1);                       // ...which refreshes it
    };
    ctx_.notify_fn = [this](const std::string& msg, bool err) {
      notice_ = {msg, err, std::chrono::steady_clock::now()};
    };
  }

  ~App() { stop_poller(); }

  int loop() {
    auto login = build_login();
    // A one-child Tab renders its child as is (a Vertical would wrap it in a
    // vbox and the page would not fill the screen).
    main_holder_ = Container::Tab({}, &holder_index_);
    auto pages = Container::Tab({login, main_holder_}, &page_);
    auto inner = dialogs_.wrap(pages);
    // A reply with a field the display did not expect (null where a number was assumed) must
    // not end the program: show what went wrong and carry on drawing.
    auto root = Renderer(inner, [inner] {
      try {
        return inner->Render();
      } catch (const std::exception& e) {
        return vbox({text(" display error: " + std::string(e.what())) | color(Color::Red) | bold,
                     text(" (press r to refresh, or switch tab)") | dim});
      }
    });
    screen_.Loop(root);
    // Quitting never waits for the server: interrupt whatever is in flight.
    poller_running_ = false;
    ctx_.abort_requests();
    if (poll_api_) poll_api_->abort();
    stop_poller();
    ctx_.stop_workers();
    if (api_) {   // a short, separate attempt to log out politely
      try {
        auto last = api_->spawn();
        last->set_read_timeout(2);
        last->logout();
      } catch (...) {
      }
    }
    return 0;
  }

 private:
  // --- login ------------------------------------------------------------------------

  Component build_login() {
    InputOption single;
    single.multiline = false;
    InputOption secret = single;
    secret.password = true;
    secret.on_enter = [this] { connect(); };
    auto host = Input(&host_, "hostname or IP", single);
    auto port = Input(&port_str_, "7420", single);
    auto password = Input(&password_, "admin password", secret);
    auto button = Button("Connect", [this] { connect(); }, ButtonOption::Ascii());
    auto form = Container::Vertical({host, port, password, button});
    password->TakeFocus();
    return Renderer(form, [=, this] {
      auto row = [](const std::string& label, Element e) {
        return hbox({text(fit(label, 10)), e | size(WIDTH, EQUAL, 40) | border});
      };
      Element msg = login_message_.empty()
                        ? text("")
                        : paragraph(login_message_) | color(Color::Red);
      return vbox({
                 text("rei-fs") | bold | center,
                 text("decentralized file and backup manager") | dim | center,
                 separator(),
                 row("Server", host->Render()),
                 row("Port", port->Render()),
                 row("Password", password->Render()),
                 button->Render() | center,
                 msg | size(WIDTH, LESS_THAN, 60),
             }) |
             border | center;
    });
  }

  void connect() {
    int port = 0;
    try {
      port = std::stoi(port_str_);
    } catch (...) {
      login_message_ = "invalid port";
      return;
    }
    std::string key = Profiles::key(host_, port);
    std::string pinned;
    if (auto it = profiles_.known_servers.find(key); it != profiles_.known_servers.end())
      pinned = it->second;
    login_message_ = "connecting...";
    screen_.RequestAnimationFrame();
    api_ = std::make_unique<Api>(host_, port, pinned);
    try {
      api_->probe();
    } catch (const UntrustedCertificate& e) {
      std::string seen = e.seen;
      if (!pinned.empty()) {
        login_message_ = "certificate mismatch";
        dialogs_.ask_choice(
            "WARNING: the certificate of " + key + " CHANGED.\n"
            "Expected " + pinned + "\nbut got  " + seen + "\n"
            "This happens if the server was reinstalled, or if someone is intercepting "
            "the connection. Check with 'reifs fingerprint' on the server.",
            {"Abort", "Trust the new certificate"}, [this, key, seen](int i) {
              if (i == 1) trust_and_login(key, seen);
            });
      } else {
        login_message_ = "";
        dialogs_.ask_choice(
            "First connection to " + key + ". The server's certificate fingerprint is\n" +
                seen + "\nCompare it with 'reifs fingerprint' on the server.",
            {"Trust and continue", "Cancel"}, [this, key, seen](int i) {
              if (i == 0) trust_and_login(key, seen);
            });
      }
      return;
    } catch (const std::exception& e) {
      login_message_ = e.what();
      return;
    }
    do_login();
  }

  void trust_and_login(const std::string& key, const std::string& fingerprint) {
    profiles_.known_servers[key] = fingerprint;
    api_->set_pinned(fingerprint);
    do_login();
  }

  void do_login() {
    try {
      api_->login(password_);
    } catch (const std::exception& e) {
      login_message_ = e.what();
      return;
    }
    login_message_.clear();
    profiles_.last_host = host_;
    profiles_.last_port = api_->port();
    try {
      profiles_.save();
    } catch (const std::exception& e) {
      notice_ = {std::string("could not save settings: ") + e.what(), true,
                 std::chrono::steady_clock::now()};
    }
    ctx_.api = api_.get();
    ctx_.connected_at = std::chrono::steady_clock::now();
    ctx_.start_workers(*api_, [this](std::function<void()> task) { screen_.Post(std::move(task)); });
    build_main();
    page_ = 1;
    start_poller();
  }

  // --- main page ----------------------------------------------------------------------

  void build_main() {
    views_ = make_views(ctx_);
    Components comps;
    for (auto& v : views_) comps.push_back(Make<AlwaysFocusable>(v->component()));
    auto tabs = Container::Tab(comps, &tab_);
    auto shell = Renderer(tabs, [this, tabs] { return render_main(tabs); });
    shell |= CatchEvent([this](Event e) { return on_global_event(e); });
    main_holder_->DetachAllChildren();
    main_holder_->Add(shell);
    shell->TakeFocus();
    switch_to(0);
  }

  void switch_to(int index) {
    if (index < 0 || index >= static_cast<int>(views_.size())) return;
    tab_ = index;
    help_offset_ = 0;
    views_[tab_]->refresh();
  }

  // The part of the key list that fits in `width` columns, starting `offset` columns in
  // (clamped), with arrows where more is hidden. [ and ] move it.
  static std::string help_window(const std::string& full, int width, int& offset) {
    int total = ftxui::string_width(full);
    if (total <= width || width < 12) {
      offset = 0;
      return full;
    }
    offset = std::max(0, std::min(offset, total - (width - 2)));
    std::string out;
    int col = 0, used = 0;
    for (const auto& g : ftxui::Utf8ToGlyphs(full)) {
      int w = ftxui::string_width(g);
      if (col < offset) {
        col += w;
        continue;
      }
      if (used + w > width - 2) break;
      out += g;
      used += w;
    }
    bool more = offset + used < total;
    return (offset > 0 ? "‹" : " ") + out + (more ? "›" : " ");
  }

  bool on_global_event(const Event& e) {
    if (dialogs_.shown()) return false;
    const Event fkeys[] = {Event::F1, Event::F2, Event::F3, Event::F4, Event::F5,
                           Event::F6, Event::F7, Event::F8, Event::F9};
    for (int i = 0; i < 9 && i < static_cast<int>(views_.size()); ++i) {
      if (e == fkeys[i]) {
        switch_to(i);
        return true;
      }
    }
    if (e == Event::Special("tick")) return true;   // just a redraw
    if (e == Event::Custom) {
      views_[tab_]->on_poll();
      return true;
    }
    if (views_[tab_]->typing()) return false;
    if (e.is_character()) {
      const std::string& c = e.character();
      if (c.size() == 1 && c[0] >= '0' && c[0] <= '9') {
        int tab = c[0] == '0' ? 9 : c[0] - '1';   // 0 is the tenth tab
        if (tab < static_cast<int>(views_.size())) switch_to(tab);
        return true;
      }
      if (c == "q") {
        dialogs_.confirm("Quit rei-fs?", [this] { screen_.Exit(); });
        return true;
      }
      if (c == "-" && views_.size() > 10) {   // the eleventh tab
        switch_to(10);
        return true;
      }
      if (c == "[" || c == "]") {   // scroll the key list at the bottom
        help_offset_ = std::max(0, help_offset_ + (c == "]" ? 24 : -24));
        return true;
      }
      if (c == "?") {
        // This tab's keys, one per line (the list at the bottom separates them by two spaces).
        std::string tab_keys, help = views_[tab_]->help();
        for (size_t pos = 0; pos <= help.size();) {
          size_t end = help.find("  ", pos);
          if (end == std::string::npos) end = help.size();
          std::string item = help.substr(pos, end - pos);
          while (!item.empty() && item.front() == ' ') item.erase(item.begin());
          if (!item.empty()) tab_keys += "  " + item + "\n";
          pos = end + 2;
        }
        dialogs_.info("Keys",
                      "1-9, 0, -     switch tab (0 is the tenth, - the eleventh)\n"
                      "arrows        move, Enter opens / chooses\n"
                      "r             refresh the current tab\n"
                      "[ ]           scroll the key list at the bottom\n"
                      "q             quit\n\nThis tab (" + views_[tab_]->title() + "):\n" + tab_keys);
        return true;
      }
    }
    return false;
  }

  Element render_main(Component tabs) {
    Elements header;
    for (size_t i = 0; i < views_.size(); ++i) {
      auto label = text(" " + (i >= 10 ? std::string("-") : std::to_string((i + 1) % 10)) + " " + views_[i]->title() + " ");
      header.push_back(static_cast<int>(i) == tab_ ? label | inverted | bold : label);
    }
    json st = ctx_.status();
    auto act = st.value("activity", json::object());
    std::string state = act.value("state", "?");
    std::string detail = act.value("detail", "");
    Elements bar = {text(" " + api_->server_name() + " ") | inverted};
    // The Overview tab has the full detail; the bar only gets a short version.
    if (!detail.empty()) {
      std::string shortened = fit(detail, 28);
      while (!shortened.empty() && shortened.back() == ' ') shortened.pop_back();
      detail = shortened;
    }
    auto activity = text(" " + state + (detail.empty() ? "" : ": " + detail) + " ");
    bar.push_back(state == "idle" ? activity : activity | color(Color::Yellow));
    auto count = [&](const char* key, const char* label, Color c) {
      int n = st.value(key, 0);
      auto t = text(" " + std::string(label) + " " + std::to_string(n) + " ");
      return n ? t | color(c) : t | dim;
    };
    bar.push_back(count("open_decisions", "decisions", Color::Yellow));
    bar.push_back(count("open_wanted", "wanted", Color::Cyan));
    bar.push_back(count("active_jobs", "jobs", Color::Cyan));
    bar.push_back(count("under_replicated", "under-replicated", Color::Red));
    if (st.value("peers_total", 0) > 0) {
      int on = st.value("peers_online", 0), total = st.value("peers_total", 0);
      auto t = text(" servers " + std::to_string(on) + "/" + std::to_string(total) + " ");
      bar.push_back(on < total ? t | color(Color::Red) : t | dim);
    }
    if (st.value("pair_requests", 0) > 0)
      bar.push_back(text(" pairing request! ") | color(Color::Yellow) | bold);
    if (!act.value("last_error", "").empty())
      bar.push_back(text(" " + act.value("last_error", "") + " ") | color(Color::Red));
    if (int n = ctx_.pending(); n > 0) {
      double waited = ctx_.oldest_pending_seconds();
      if (waited >= 0.5) {
        char buf[64];
        std::snprintf(buf, sizeof buf, " waiting for the server (%d, %.0f s) ", n, waited);
        auto t = text(buf);
        bar.push_back(waited > 5 ? t | color(Color::Red) | bold : t | color(Color::Yellow));
      }
    }
    if (auto err = poll_error(); !err.empty())
      bar.push_back(text(" " + err + " ") | color(Color::Red));

    Element notice = text("");
    auto age = std::chrono::steady_clock::now() - notice_.at;
    if (!notice_.text.empty() && age < std::chrono::seconds(notice_.error ? 12 : 6)) {
      notice = text(" " + notice_.text);
      notice = notice_.error ? notice | color(Color::Red) : notice | color(Color::Green);
    }
    return vbox({
        hbox(header),
        separator(),
        tabs->Render() | flex,
        separator(),
        notice,
        text(help_window(" " + views_[tab_]->help(),
                         std::max(20, ftxui::Terminal::Size().dimx - 22), help_offset_) +
             "   [ ]: scroll  ?: keys  q: quit") | dim,
        hbox(bar),
    });
  }

  // --- status polling -------------------------------------------------------------

  void start_poller() {
    poller_running_ = true;
    poll_api_ = api_->spawn();
    poll_api_->set_read_timeout(10);   // the status line should say "not answering", not freeze
    // Two threads: the clock must keep ticking (the "waiting for the server" and
    // "working for ..." counters) even while a status request is stuck.
    ticker_ = std::thread([this] {
      for (int tick = 0; poller_running_; ++tick) {
        screen_.PostEvent(tick % 2 == 0 ? Event::Custom : Event::Special("tick"));
        std::unique_lock lock(poller_mutex_);
        poller_cv_.wait_for(lock, std::chrono::seconds(1), [this] { return !poller_running_; });
      }
    });
    poller_ = std::thread([this] {
      while (poller_running_) {
        poll_status();
        screen_.PostEvent(Event::Custom);
        std::unique_lock lock(poller_mutex_);
        poller_cv_.wait_for(lock, std::chrono::seconds(2), [this] { return !poller_running_; });
      }
    });
  }

  void poll_status() {
    try {
      ctx_.set_status(poll_api_->get("/api/status"));
      poll_failures_ = 0;
      set_poll_error("");
    } catch (const ApiError& e) {
      if (e.status == 401) {
        // The server restarted and forgot our session: log in again (the
        // new token is shared by every connection).
        try {
          poll_api_->login(password_);
          set_poll_error("");
        } catch (const std::exception& e2) {
          set_poll_error(std::string("session lost: ") + e2.what());
        }
      } else if (++poll_failures_ >= 3) {   // one stumble is not worth a red message
        set_poll_error(e.what());
      }
    } catch (const std::exception& e) {
      if (++poll_failures_ >= 3) set_poll_error(e.what());
    }
  }

  void set_poll_error(const std::string& e) { ctx_.set_poll_error(e); }
  std::string poll_error() { return ctx_.poll_error(); }

  void stop_poller() {
    {
      std::lock_guard lock(poller_mutex_);
      poller_running_ = false;
    }
    poller_cv_.notify_all();
    if (poller_.joinable()) poller_.join();
    if (ticker_.joinable()) ticker_.join();
  }

  ScreenInteractive screen_;
  Profiles profiles_;
  Dialogs dialogs_;
  Context ctx_;
  std::unique_ptr<Api> api_;
  std::vector<std::shared_ptr<View>> views_;
  Component main_holder_;
  int page_ = 0;
  int holder_index_ = 0;
  int tab_ = 0;
  int help_offset_ = 0;   // how far the key list at the bottom is scrolled
  std::string host_, port_str_, password_, login_message_;
  Notice notice_;
  std::unique_ptr<Api> poll_api_;
  std::thread poller_, ticker_;
  int poll_failures_ = 0;   // consecutive failed status polls (poller thread only)
  std::atomic<bool> poller_running_{false};
  std::mutex poller_mutex_;
  std::condition_variable poller_cv_;
};

// "host", "host:port", "[v6addr]" or "[v6addr]:port"; port 0 = not given.
std::pair<std::string, int> split_host(const std::string& arg) {
  std::string host = arg, port;
  if (!arg.empty() && arg[0] == '[') {
    auto close = arg.find(']');
    if (close == std::string::npos) return {arg, 0};
    host = arg.substr(1, close - 1);
    if (close + 1 < arg.size() && arg[close + 1] == ':') port = arg.substr(close + 2);
  } else if (std::count(arg.begin(), arg.end(), ':') == 1) {
    auto colon = arg.find(':');
    host = arg.substr(0, colon);
    port = arg.substr(colon + 1);
  }
  try {
    return {host, port.empty() ? 0 : std::stoi(port)};
  } catch (...) {
    return {host, 0};
  }
}

void usage() {
  std::cout << "usage: rei-fs [HOST[:PORT]]\n"
               "       rei-fs --fingerprint HOST[:PORT]   show the server's certificate "
               "fingerprint\n"
               "       rei-fs --version\n";
}

}  // namespace

int run(int argc, char** argv) {
  std::vector<std::string> args(argv + 1, argv + argc);
  if (!args.empty() && (args[0] == "-h" || args[0] == "--help")) {
    usage();
    return 0;
  }
  if (!args.empty() && args[0] == "--version") {
    std::cout << "rei-fs client 0.1.0\n";
    return 0;
  }
  if (!args.empty() && args[0] == "--fingerprint") {
    if (args.size() != 2) {
      usage();
      return 2;
    }
    auto [host, port] = split_host(args[1]);
    Api api(host, port ? port : 7420);
    try {
      api.probe();
    } catch (const UntrustedCertificate& e) {
      std::cout << e.seen << "\n";
      return 0;
    } catch (const std::exception& e) {
      std::cerr << "rei-fs: " << e.what() << "\n";
      return 1;
    }
    return 1;
  }
  if (args.size() > 1) {
    usage();
    return 2;
  }
  auto [host, port] = args.empty() ? std::pair<std::string, int>{"", 0} : split_host(args[0]);
  App app(host, port);
  return app.loop();
}

}  // namespace reifs
