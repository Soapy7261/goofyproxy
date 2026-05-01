export const CONFIG = {
    // where to store user data
    USERS_DIR: './.users',

    // default HTML page
    DEFAULT_HTML_PATH: './public/index.html',

    // SSL certificate and key, only required for HTTPS.
    SSL_KEY_PATH: './.certs/server.key',
    SSL_CERT_PATH: './.certs/server.crt',

    // bincall API path
    BINCALL_API_PATH: '/bincall',

    // HTTPS server port. use null to disable the HTTPS server.
    HTTPS_PORT: 443,

    // HTTP server port. use null to disable the HTTP server.
    HTTP_PORT: 80,

    // if true, the HTTP server will reject requests and ask the client to use
    // HTTPS instead.
    HTTP_REJECT: true,

    CALL_TIMEOUT_MS: 60000,
    CALL_ANSWER_POLL_INTERVAL_MS: 1000,
    PACKET_POLL_INTERVAL_MS: 80,
    MAX_USERNAME_LENGTH: 64,
    MIN_PASSWORD_LENGTH: 10,
    MAX_PASSWORD_LENGTH: 64,
    MAX_PASSWORD_ADJACENT_IDENTICAL_CHARS: 3,
} as const;
