// Application shell: login page, tabs, status bar, dialogs.
#pragma once

#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <ftxui/component/component.hpp>

#include "api.hpp"

namespace reifs {

// One modal dialog at a time, shared by every view. Callbacks run after the
// dialog closed (posted to the UI loop), so they may open another dialog.
class Dialogs {
 public:
  Dialogs();
  ftxui::Component wrap(ftxui::Component main);

  void ask_text(const std::string& title, const std::string& initial,
                std::function<void(const std::string&)> on_ok);
  void ask_choice(const std::string& title, std::vector<std::string> options,
                  std::function<void(int)> on_ok);
  void confirm(const std::string& question, std::function<void()> on_yes);
  void info(const std::string& title, const std::string& body);
  bool shown() const { return shown_; }

 private:
  void open(ftxui::Component content);
  void finish(std::function<void()> then);

  bool shown_ = false;
  ftxui::Component holder_;
  std::string text_;
  std::vector<std::string> options_;
  int choice_ = 0;
};

// What views share: the API, dialogs, notifications and the latest status.
struct Context {
  Api* api = nullptr;
  Dialogs* dialogs = nullptr;
  std::function<void(const std::string&, bool)> notify_fn;

  void notify(const std::string& msg) { notify_fn(msg, false); }
  void error(const std::string& msg) { notify_fn(msg, true); }
  // Run an API action; report failures and return false.
  bool attempt(const std::function<void()>& fn);
  std::vector<std::string> node_names();

  json status() const;
  void set_status(json s);

 private:
  mutable std::mutex status_mutex_;
  json status_ = json::object();
};

class View {
 public:
  virtual ~View() = default;
  virtual std::string title() const = 0;
  virtual ftxui::Component component() = 0;
  virtual void refresh() = 0;
  virtual std::string help() const = 0;
  // True while a text field has focus (global letter/digit keys are off).
  virtual bool typing() const { return false; }
  // Called after every status poll while the view is visible.
  virtual void on_poll() {}
};

std::vector<std::shared_ptr<View>> make_views(Context& ctx);

int run(int argc, char** argv);

}  // namespace reifs
