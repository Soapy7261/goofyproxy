export const CONFIG = {
    USERS_DIR: './users',
    DEFAULT_HTML_PATH: './public/index.html',
    SSL_KEY_PATH: './certs/server.key',
    SSL_CERT_PATH: './certs/server.crt',
    WSIO_PATH_IN_REQUEST: '/wsio-carrot',
    HTTPS_PORT: 443,
    HTTP_PORT: 80, // or null to disable the HTTP server
    CALL_TIMEOUT_MS: 60000,
    CALL_ANSWER_POLL_INTERVAL_MS: 1000,
    PACKET_POLL_INTERVAL_MS: 80,
    MAX_USERNAME_LENGTH: 64,
    MIN_PASSWORD_LENGTH: 10,
    MAX_PASSWORD_LENGTH: 64,
    MAX_ADJACENT_IDENTICAL_CHARS: 3,
} as const;
