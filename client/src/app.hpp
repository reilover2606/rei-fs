// Application shell: login page, tabs, status bar, dialogs.
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
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
  int cursor_ = 0;
  std::vector<std::string> options_;
  int choice_ = 0;
};

// Runs jobs one after the other on a background thread. A job submitted with
// a key replaces a job with the same key that is still waiting (so that, for
// example, only the detail of the item the user stopped on gets fetched).
class Executor {
 public:
  Executor();
  ~Executor();
  Executor(const Executor&) = delete;
  Executor& operator=(const Executor&) = delete;
  void submit(const std::string& key, std::function<void()> job,
              std::function<void()> on_dropped = {});
  void stop();  // drops waiting jobs, waits for the running one

 private:
  struct Item {
    std::string key;
    std::function<void()> job, on_dropped;
  };
  void loop();
  std::mutex mutex_;
  std::condition_variable wake_;
  std::vector<Item> queue_;
  bool stopping_ = false;
  std::thread thread_;
};

// What views share: the API, dialogs, notifications and the latest status.
//
// Server calls never run on the interface thread: `get` and `send` (or `run`
// for several steps) do the work on a background thread and call `done` back
// on the interface thread with the result. A failure is shown as a notice and
// `done` is not called. Reads and writes use separate connections, so a write
// waiting for the server cannot hold up reading.
struct Context {
  const Api* api = nullptr;  // read-only: the session's name and id (const, so that a view
                             // cannot call get/post on the interface thread)
  Dialogs* dialogs = nullptr;
  std::function<void(const std::string&, bool)> notify_fn;
  // Switch to the Tree tab showing a directory (and selecting an entry in it).
  std::function<void(const std::string&, const std::string&)> show_in_tree;

  void start_workers(Api& main, std::function<void(std::function<void()>)> post);
  void stop_workers();
  void run(bool write, std::function<json(Api&)> work, std::function<void(json)> done,
           const std::string& key = "", std::function<void()> on_error = {});
  void get(const std::string& path, std::function<void(json)> done, const std::string& key = "");
  void send(const std::string& path, json body = json::object(),
            std::function<void(json)> done = {});
  // Requests not answered yet, and how long the oldest has been waiting.
  int pending() const;
  double oldest_pending_seconds() const;

  void notify(const std::string& msg) { notify_fn(msg, false); }
  void error(const std::string& msg) { notify_fn(msg, true); }

  json status() const;
  void set_status(json s);
  // Last error of the status poll ("" when fine), and when polling started.
  std::string poll_error() const;
  void set_poll_error(const std::string& e);
  std::chrono::steady_clock::time_point connected_at = std::chrono::steady_clock::now();

 private:
  void finish(std::uint64_t id);

  mutable std::mutex status_mutex_;
  json status_ = json::object();
  std::string poll_error_;

  std::unique_ptr<Api> read_api_, write_api_;
  Executor read_exec_, write_exec_;
  std::function<void(std::function<void()>)> post_;
  std::atomic<bool> closing_{false};
  mutable std::mutex pending_mutex_;
  std::map<std::uint64_t, std::chrono::steady_clock::time_point> pending_;
  std::uint64_t next_id_ = 0;
};

class View {
 public:
  virtual ~View() = default;
  virtual std::string title() const = 0;
  virtual ftxui::Component component() = 0;
  virtual void refresh() = 0;
  virtual std::string help() const = 0;
  // Show `dir` (and select `select` in it, if given). Only the Tree uses it.
  virtual void navigate(const std::string& dir, const std::string& select = "") {
    (void)dir;
    (void)select;
  }
  // True while a text field has focus (global letter/digit keys are off).
  virtual bool typing() const { return false; }
  // Called after every status poll while the view is visible.
  virtual void on_poll() {}
};

std::vector<std::shared_ptr<View>> make_views(Context& ctx);

int run(int argc, char** argv);

}  // namespace reifs
