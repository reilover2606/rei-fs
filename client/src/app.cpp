#include "app.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <iostream>
#include <thread>

#include <ftxui/component/component_options.hpp>
#include <ftxui/component/event.hpp>
#include <ftxui/component/screen_interactive.hpp>
#include <ftxui/dom/elements.hpp>

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
    if (auto* screen = ScreenInteractive::Active())
      screen->Post(std::move(then));
    else
      then();
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

// --- context -----------------------------------------------------------------------

bool Context::attempt(const std::function<void()>& fn) {
  try {
    fn();
    return true;
  } catch (const UntrustedCertificate&) {
    error("the server certificate changed; reconnect to review it");
  } catch (const std::exception& e) {
    error(e.what());
  }
  return false;
}

std::vector<std::string> Context::node_names() {
  std::vector<std::string> names;
  attempt([&] {
    json out = api->get("/api/nodes");  // keep the temporary alive for the loop
    for (const auto& n : out["nodes"]) {
      std::string label = n.value("name", "");
      if (!n.value("online", false)) label += " (offline)";
      names.push_back(label);
    }
  });
  return names;
}

json Context::status() const {
  std::lock_guard lock(status_mutex_);
  return status_;
}

void Context::set_status(json s) {
  std::lock_guard lock(status_mutex_);
  status_ = std::move(s);
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
    auto root = dialogs_.wrap(pages);
    screen_.Loop(root);
    if (api_) api_->logout();
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
    views_[tab_]->refresh();
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
    if (e == Event::Custom) {
      views_[tab_]->on_poll();
      return true;
    }
    if (views_[tab_]->typing()) return false;
    if (e.is_character()) {
      const std::string& c = e.character();
      if (c.size() == 1 && c[0] >= '1' && c[0] <= '9') {
        switch_to(c[0] - '1');
        return true;
      }
      if (c == "q") {
        dialogs_.confirm("Quit rei-fs?", [this] { screen_.Exit(); });
        return true;
      }
      if (c == "?") {
        dialogs_.info("Keys",
                      "1-9 / F1-F9   switch tab\n"
                      "arrows        move, Enter opens / chooses\n"
                      "r             refresh the current tab\n"
                      "q             quit\n\n"
                      "Each tab lists its own keys at the bottom of the screen.");
        return true;
      }
    }
    return false;
  }

  Element render_main(Component tabs) {
    Elements header;
    for (size_t i = 0; i < views_.size(); ++i) {
      auto label = text(" " + std::to_string(i + 1) + " " + views_[i]->title() + " ");
      header.push_back(static_cast<int>(i) == tab_ ? label | inverted | bold : label);
    }
    json st = ctx_.status();
    auto act = st.value("activity", json::object());
    std::string state = act.value("state", "?");
    std::string detail = act.value("detail", "");
    Elements bar = {text(" " + api_->server_name() + " ") | inverted};
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
        text(" " + views_[tab_]->help() + "   ?: keys  q: quit") | dim,
        hbox(bar),
    });
  }

  // --- status polling -------------------------------------------------------------

  void start_poller() {
    poller_running_ = true;
    poller_ = std::thread([this] {
      while (poller_running_) {
        try {
          ctx_.set_status(api_->get("/api/status"));
          set_poll_error("");
        } catch (const ApiError& e) {
          if (e.status == 401) {
            // The server restarted and forgot our session: log in again.
            try {
              api_->login(password_);
              set_poll_error("");
            } catch (const std::exception& e2) {
              set_poll_error(std::string("session lost: ") + e2.what());
            }
          } else {
            set_poll_error(e.what());
          }
        } catch (const std::exception& e) {
          set_poll_error(e.what());
        }
        screen_.PostEvent(Event::Custom);
        std::unique_lock lock(poller_mutex_);
        poller_cv_.wait_for(lock, std::chrono::seconds(2), [this] { return !poller_running_; });
      }
    });
  }

  void set_poll_error(const std::string& e) {
    std::lock_guard lock(poll_error_mutex_);
    poll_error_ = e;
  }
  std::string poll_error() {
    std::lock_guard lock(poll_error_mutex_);
    return poll_error_;
  }

  void stop_poller() {
    {
      std::lock_guard lock(poller_mutex_);
      poller_running_ = false;
    }
    poller_cv_.notify_all();
    if (poller_.joinable()) poller_.join();
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
  std::string host_, port_str_, password_, login_message_;
  std::string poll_error_;
  std::mutex poll_error_mutex_;
  Notice notice_;
  std::thread poller_;
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
