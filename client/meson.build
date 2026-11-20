project(
  'rei-fs-client',
  'cpp',
  version: '0.1.0',
  license: 'MIT',
  meson_version: '>=1.1.0',
  default_options: ['cpp_std=c++20', 'warning_level=2', 'buildtype=release'],
)

# Dependencies come from the system when suitable, otherwise from the wraps
# in subprojects/ (downloaded on first configure).
ftxui_dep = [
  dependency('ftxui-screen', fallback: ['ftxui', 'screen_dep'],
             default_options: ['examples=disabled']),
  dependency('ftxui-dom', fallback: ['ftxui', 'dom_dep']),
  dependency('ftxui-component', fallback: ['ftxui', 'component_dep']),
]
json_dep = dependency('nlohmann_json', version: '>=3.11',
                      fallback: ['nlohmann_json', 'nlohmann_json_dep'])
# OpenSSL >= 3: on Windows install it with MSYS2 (mingw-w64-ucrt-x86_64-openssl)
# or vcpkg; see README.md.
openssl_dep = dependency('openssl', version: '>=3.0')
httplib_dep = dependency('cpp-httplib', version: '>=0.66',
                         fallback: ['cpp-httplib', 'cpp_httplib_dep'],
                         default_options: ['tls=enabled', 'zlib=disabled',
                                           'brotli=disabled', 'zstd=disabled'])
thread_dep = dependency('threads')

deps = [ftxui_dep, json_dep, openssl_dep, httplib_dep, thread_dep]
if host_machine.system() == 'windows'
  deps += [meson.get_compiler('cpp').find_library('ws2_32'),
           meson.get_compiler('cpp').find_library('crypt32')]
  add_project_arguments('-DUNICODE', '-D_UNICODE', '-DNOMINMAX',
                        '-D_WIN32_WINNT=0x0A00', language: 'cpp')
endif

core = static_library(
  'reifs-core',
  'src/api.cpp',
  'src/profiles.cpp',
  'src/format.cpp',
  dependencies: deps,
)

executable(
  'rei-fs',
  'src/main.cpp',
  'src/app.cpp',
  'src/views.cpp',
  link_with: core,
  dependencies: deps,
  install: true,
)

test('core', executable('core-tests', 'tests/core_tests.cpp', link_with: core,
                        dependencies: deps))
