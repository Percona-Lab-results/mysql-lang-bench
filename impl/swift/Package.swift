// swift-tools-version:5.9
// mysql-lang-bench — Swift implementation (v3, four micro-workloads). Uses the
// libmysqlclient C API via the CMySQL system-library module and OpenSSL's MD5
// (libcrypto) via the CCrypto system-library module. Ubuntu's libmysqlclient-dev
// ships no mysqlclient.pc, so both module maps name their headers by absolute
// path instead of relying on pkg-config.
import PackageDescription

let package = Package(
    name: "bench",
    targets: [
        .systemLibrary(
            name: "CMySQL",
            path: "Sources/CMySQL"
        ),
        .systemLibrary(
            name: "CCrypto",
            path: "Sources/CCrypto"
        ),
        .executableTarget(
            name: "bench",
            dependencies: ["CMySQL", "CCrypto"],
            path: "Sources/bench",
            linkerSettings: [
                .linkedLibrary("mysqlclient"),
                .linkedLibrary("crypto"),
            ]
        ),
    ]
)
