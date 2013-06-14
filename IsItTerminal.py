# coding=utf-8
import sublime
import sublime_plugin

import os
import fnmatch
import time
import re
import json
import queue
import hashlib
from .its_terminal import TerminalConnectionWorker


class IsItTerminalCommand(sublime_plugin.TextCommand):

    items = {}
    edit = None
    server = None
    servers = {}
    serverName = None
    settings = None
    settingFile = "IsItTerminal.sublime-settings"
    lastDir = None
    timeout = 10
    connector = None

    def run(self, edit, action=None, text=None):
        # Check if we have a send that itData is set
        # if not then back to whatever else we were doing lickety split
        itData = self.view.settings().get("itData", {})
        if action == "send":
            if itData:
                self.send(itData)
            else:
                return
        self.edit = edit
        # Ensure that the self.servers dict is populated
        self.load_server_list()
        # Load the connector
        if not self.connector:
            self.connector = IsItTerminalConnector(self.view)
        if action == "print":
            self.print_it(text)
        elif self.serverName:
            # Fire up the self.serverName server
            self.start_server(self.serverName)
        else:
            # Lastly, no server has yet been selected, display the server list and
            # other options
            self.items = [name for name in sorted(self.servers)]
            items = [[
                "  %s (%s)" % (name, self.servers[name]["settings"]["host"]),
                "  User: %s, Path: %s" % (
                    self.servers[name]["settings"]["user"],
                    self.servers[name]["settings"]["remote_path"]
                )
            ] for name in self.items]
            items.insert(0, [
                "» Quick connect",
                "Just enter a host and a username / password"
            ])
            items.insert(0, [
                "» Add a new server",
                "Complete new server details to quickly connect in future"
            ])
            self.show_quick_panel(items, self.handle_server_select)

    def load_server_list(self):
        if self.servers:
            return
        # Load all files in User/IsItTerminal/Servers folder
        serverConfigPath = self.get_server_config_path()
        if not os.path.exists(serverConfigPath):
            try:
                os.makedirs(serverConfigPath)
            except:
                pass
        for root, dirs, files in os.walk(serverConfigPath):
            for filename in fnmatch.filter(files, "*"):
                serverName = filename[0:filename.rfind(".")]
                self.servers[serverName] = {}
                self.servers[serverName]["path"] = os.path.join(root, filename)
                self.servers[serverName]["settings"] = self.jsonify(
                    open(self.servers[serverName]["path"]).read()
                )

    def handle_server_select(self, selection):
        if selection is -1:
            return
        elif selection is 0:
            # User has requested to add a new server
            # Open a new tab and populate it with the defult new server snippet
            saveTo = self.get_server_config_path()
            snippet = sublime.load_resource(
                "Packages/IsItTerminal/NewServer.default-config"
            )
            newSrv = sublime.active_window().new_file()
            newSrv.set_name("NewServer.sublime-settings")
            newSrv.set_syntax_file("Packages/JavaScript/JSON.tmLanguage")
            newSrv.settings().set("default_dir", saveTo)
            self.insert_snippet(snippet)
        elif selection is 1:
            # User has requested to quick connect
            self.show_input_panel(
                "Enter connection string (user@hostname:port/remote/path): ",
                "",
                self.handle_quick_host,
                self.handle_change,
                self.handle_cancel
            )
        else:
            # A server has been selected from the list
            self.start_server(self.items[selection - 2])
            self.run_ssh_command("", callback=self.print_it_callback)

    def insert_snippet(self, snippet):
        view = self.view
        if view.is_loading():
            sublime.set_timeout(lambda: self.insert_snippet(snippet), 100)
        else:
            view.run_command("insert_snippet", {"contents": snippet})

    def start_server(self, serverName, quickConnect=False):
        try:
            if self.serverName != serverName:
                self.serverName = serverName
                self.server = self.servers[serverName]
                self.lastDir = self.get_server_setting("remote_path", None)
            if not quickConnect:
                self.server = self.servers[self.serverName]
        except Exception as e:
            debug("Exception when gathering server settings for %s: %s" % (
                self.serverName, e
            ))
            self.serverName = None
            self.run()
            return
        self.open_server()

    def open_server(self):
        itData = self.view.settings().get("itData", {})
        if itData:
            if self.serverName != itData["serverName"]:
                itData["path"] = self.lastDir = self.get_server_setting(
                    "/home/%s" % self.get_server_setting("user")
                )
            else:
                self.lastDir = itData["path"]
        else:
            itData["path"] = self.lastDir = self.get_server_setting(
                "/home/%s" % self.get_server_setting("user")
            )
            itData["serverName"] = self.serverName
        self.view.settings().set("itData", itData)

    def print_it_callback(self, results, cP=None):
        sublime.active_window().run_command(
            "is_it_terminal",
            {"action": "print", "text": results["out"]}
        )

    def print_it(self, text):
        itData = self.view.settings().get("itData", {})
        view = self.view
        # Get the first highlighted string to search for
        selected = view.sel()[0]
        i = view.insert(self.edit, selected.b, "\n" + self.tidy(text) + " ")
        itData["pos"] = selected.b + i
        view.show(itData["pos"])
        self.view.settings().set("itData", itData)

    def tidy(self, text):
        return "\n".join(map(self.strip, text.split("\n")))

    def strip(self, text):
        return re.sub("\\x1b[\\[\\]]+[0-9]{1,2};*[0-9]*m*", "", text.rstrip().replace("\x07", " "))

    def send(self, itData):
        view = self.view
        selected = view.sel()[0]
        cmd = view.substr(sublime.Region(itData["pos"], selected.b))
        if cmd[0:3] == "rm " or " rm " in cmd:
            self.error_message("Hold off on the rm'ming for the moment sir")
        self.view.settings().set("itData", itData)
        self.run_ssh_command(cmd, callback=self.print_it_callback)

    def handle_server_info(self, results):
        if "host_unknown" in results:
            if sublime.ok_cancel_dialog(
                "IMPORTANT! This host has not been seen before, would you like to PERMANENTLY record its fingerprint for later connections?",
                "Yes, store the server fingerprint"
            ):
                cmd = "echo $SHELL; grep --version; ls --version"
                self.run_ssh_command(cmd, callback=self.handle_server_info, acceptNew=True)

    def save_server_settings(self, server, settings):
        sSettings = self.get_settings()
        for settingKey in settings:
            sSettings.set(self.serverName + ":" + settingKey, settings[settingKey])
        sublime.save_settings(self.settingFile)

    def handle_quick_host(self, cs):
        self.server = {}
        self.server["settings"] = {}
        ss = self.server["settings"]
        if "/" in cs:
            (cs, ss["remote_path"]) = cs.split("/", 1)
            ss["remote_path"] = "/" + ss["remote_path"]
        else:
            ss["remote_path"] = "/"
        if ":" in cs:
            (cs, ss["port"]) = cs.split(":")
        else:
            ss["port"] = "22"
        if "@" in cs:
            (ss["user"], ss["host"]) = cs.split("@")
        else:
            ss["user"] = "root"
            ss["host"] = cs

        self.serverName = ss["host"]
        self.show_input_panel(
            "Enter password (blank to attempt pageant auth: ",
            "",
            self.handle_quick_password,
            self.handle_change,
            self.handle_cancel
        )

    def handle_quick_password(self, password):
        self.server["settings"]["password"] = password
        self.start_server(self.serverName, True)

    def get_server_setting(self, key, default=None):
        try:
            val = self.server["settings"][key]
        except:
            val = default
        return val

    def remove_comments(self, text):
        """Thanks to: http://stackoverflow.com/questions/241327/"""
        def replacer(match):
            s = match.group(0)
            if s.startswith('/'):
                return ""
            else:
                return s
        pattern = re.compile(
            r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
            re.DOTALL | re.MULTILINE
        )
        return re.sub(pattern, replacer, text)

    def jsonify(self, data):
        """Return a dict from the passed string of json"""
        self.lastJsonifyError = None
        try:
            # Remove any comments from the files as they're not technically
            # valid JSON and the parser falls over on them
            data = self.remove_comments(data)
            return json.loads(data, strict=False)
        except Exception as e:
            self.lastJsonifyError = "Error parsing JSON: %s" % str(e)
            debug(self.lastJsonifyError)
            return False

    def get_server_config_path(self):
        return os.path.join(
            sublime.packages_path(),
            "User",
            "IsItTerminal",
            "Servers"
        )

    def get_settings(self):
        if not self.settings:
            self.settings = sublime.load_settings(self.settingFile)
        return self.settings

    def show_quick_panel(self, options, done):
        sublime.set_timeout(
            lambda: sublime.active_window().show_quick_panel(options, done),
            10
        )

    def show_input_panel(self, caption, initialtext, done, change, cancel):
        sublime.set_timeout(
            lambda: sublime.active_window().show_input_panel(
                caption,
                initialtext,
                done,
                change,
                cancel
            ),
            10
        )

    def handle_change(self, selection):
        return

    def handle_cancel(self):
        return

    def split_path(self, path):
        return os.path.split(path.rstrip("/"))

    def join_path(self, path, folder):
        if not path or path[-1] is not "/":
            path = path + "/"
        newPath = "%s%s" % (path, folder)
        return newPath.rstrip("/")

    def error_message(self, msg, useLastError=False):
        if useLastError and self.lastErr:
            return sublime.error_message(self.lastErr)
        sublime.error_message(msg)
        return False

    def success_message(self, msg):
        sublime.message_dialog(msg)
        return True

    def escape_remote_path(self, path):
        if " " in path:
            return '"%s"' % path.replace('"', '""')
        else:
            return path.replace('"', '""')

    def escape_local_path(self, path):
        replacements = [
            ["<", "{"],
            [">", "}"],
            [":", ";"],
            ["\"", "'"],
            ["/", "_"],
            ["\\", "_"],
            ["|", "_"],
            ["?", "~"],
            ["*", "+"]
        ]
        for r in replacements:
            path = path.replace(r[0], r[1])
        return path

    def make_local_folder(self):
        # file selected, ensure local folder is available
        localFolder = self.get_local_tmp_path()
        for f in self.lastDir.split("/"):
            if f:
                localFolder = os.path.join(
                    localFolder,
                    f
                )
        try:
            os.makedirs(localFolder)
        except FileExistsError:
            pass
        return localFolder

    def run_ssh_command(
        self,
        cmd,
        checkReturn=None,
        listenAttempts=1,
        timeout=None,
        callback=None,
        cP=None,
        dropResults=False,
        acceptNew=False
    ):
        return self.connector.run_remote_command(
            cmd,
            checkReturn,
            listenAttempts,
            timeout,
            callback,
            cP,
            dropResults,
            acceptNew,
            serverName=self.serverName,
            serverSettings=self.server["settings"]
        )


class IsItTerminalConnector(object):
    appResults = {}
    sshQueue = None
    sshThreads = []
    view = None
    timeout = 10

    def __init__(self, view):
        self.view = view
        # Fire up a ssh and sftp thread and queue. Will immediately block the
        # queue waiting on first job.
        if not self.sshQueue:
            self.sshQueue = queue.Queue()
        if not self.sshThreads:
            self.create_ssh_thread()

    def create_ssh_thread(self):
        key = len(self.sshThreads)
        self.sshThreads.append(
            TerminalConnectionWorker.TerminalConnectionWorker()
        )
        self.sshThreads[key].start()
        self.sshThreads[key].config(
            key,
            self.sshQueue,
            self.appResults
        )

    def __del__(self):
        debug("__del__ called")
        self.remove_ssh_thread(len(self.sshThreads))

    def remove_ssh_thread(self, threadsToRemove=1):
        tc = len(self.sshThreads)
        if tc > 0:
            index = tc - 1
            threadsToRemove -= 1
            debug("Popping ssh")
            self.sshThreads.pop()
            # We need to send tc * messages down the wire so that each thread
            # gets a copy of the message. The threadId of each thread is its
            # key on the list.
            for i in range(tc):
                self.sshQueue.put({"KILL": index})
            if threadsToRemove > 0:
                self.remove_ssh_thread(threadsToRemove)

    def run_remote_command(
        self,
        cmd,
        checkReturn,
        listenAttempts=1,
        timeout=None,
        callback=None,
        cP=None,
        dropResults=False,
        acceptNew=False,
        serverName=None,
        serverSettings=None
    ):
        debug("run command called with cmd: \"%s\"" % (
            cmd
        ))
        if timeout is None:
            timeout = self.timeout
        expireTime = time.time() + timeout
        work = {}
        work["server_name"] = serverName
        work["settings"] = serverSettings
        work["cmd"] = cmd
        work["prompt_contains"] = checkReturn
        work["listen_attempts"] = listenAttempts
        work["drop_results"] = dropResults
        work["expire_at"] = expireTime
        work["accept_new_host"] = acceptNew
        # Generate a unique key to listen for results on
        m = hashlib.md5()
        m.update(("%s%s" % (cmd, str(time.time()))).encode('utf-8'))
        key = m.hexdigest()
        work["key"] = key
        self.sshQueue.put(work)
        debug("....now on the queue.....")
        if callback:
            debug("Calling set_timeout to check for results")
            # TODO: This should be totally events driven, have a thread block
            # on a queue and on return of data call a callback. Once we've
            # moved at least a bit towards that from where we are now it should
            # be a much easier task. For now we'll just have to check the
            # results dict regularly with set timeouts.
            sublime.set_timeout(
                lambda: self.handle_callbacks(
                    key,
                    expireTime,
                    callback,
                    cP
                ),
                100
            )
            return
        elif dropResults:
            return
        while True:
            if time.time() > expireTime:
                debug("Timeout")
                return False
            if key in self.appResults:
                results = self.appResults[key]
                del self.appResults[key]
                debug("Result found for cmd: %s" % cmd)
                break
            else:
                time.sleep(0.1)
        if not callback:
            return results["success"]

    def handle_callbacks(self, key, expireTime, callback, cP, statusState=0, statusDir=1):
        # TODO: If "password: " in self out then password wrong, show error message
        before = statusState % 8
        after = 7 - before
        if not after:
            statusDir = -1
        elif not before:
            statusDir = 1
        statusState += statusDir
        self.view.set_status("remoteedit", "RemoteEdit [%s=%s]" % (" " * before, " " * after))
        if key in self.appResults:
            self.view.set_status("remoteedit", "")
            debug("Results found in callback handler, firing the callback")
            results = self.appResults[key]
            del self.appResults[key]
            if cP is None:
                callback(results)
            else:
                callback(results, cP)
        elif time.time() > expireTime:
            self.view.set_status("remoteedit", "")
            if cP is None:
                callback(
                    {"success": False, "out": "", "err": ""}
                )
            else:
                callback(
                    {"success": False, "out": "", "err": ""},
                    cP
                )
        else:
            sublime.set_timeout(
                lambda: self.handle_callbacks(
                    key,
                    expireTime,
                    callback,
                    cP,
                    statusState,
                    statusDir
                ),
                100
            )


# def plugin_loaded():
#     sublime.active_window().run_command(
#         "remote_edit",
#         {"action": "on_app_start"}
#     )


def debug(data):
    if len(data) > 3000:
        print("MAIN %s: %s" % (time.strftime("%H:%M:%S"), data[0:3000]))
    else:
        print("MAIN %s: %s" % (time.strftime("%H:%M:%S"), data))
