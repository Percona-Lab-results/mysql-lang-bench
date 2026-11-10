// swift-tools-version:5.9
// mysql-lang-bench — Swift implementation. Uses the libmysqlclient C API via
// the CMySQL system-library module (Ubuntu's libmysqlclient-dev ships no
// mysqlclient.pc, so the module map names the header by absolute path instead
// of relying on pkg-config).
import PackageDescription

let package = Package(
    name: "bench",
    targets: [
        .systemLibrary(
            name: "CMySQL",
            path: "Sources/CMySQL"
        ),
        .executableTarget(
            name: "bench",
            dependencies: ["CMySQL"],
            path: "Sources/bench",
            linkerSettings: [
                .linkedLibrary("mysqlclient"),
            ]
        ),
    ]
)
