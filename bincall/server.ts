import * as https from 'https';
import * as http from 'http';
import * as fs from 'fs';
import * as WebSocket from 'ws';
import { IncomingMessage, ServerResponse } from 'http';
import { URL } from 'url';
import { CONFIG } from './config';
import { decodeAuth } from './utils/auth';
import { validateUserId, validatePassword } from './utils/validation';
import { UserService } from './services/userService';
import { handleCallConnection, handlePickupConnection } from './routes/websocketHandler';

const userService = new UserService();

// Read default HTML page
const defaultHtml = fs.readFileSync(CONFIG.DEFAULT_HTML_PATH, 'utf-8');

// Read SSL certificate and key
let sslOptions: https.ServerOptions | null = null
if (CONFIG.HTTPS_PORT != null
    && CONFIG.SSL_KEY_PATH != null
    && CONFIG.SSL_CERT_PATH != null) {
    sslOptions = {
        key: fs.readFileSync(CONFIG.SSL_KEY_PATH),
        cert: fs.readFileSync(CONFIG.SSL_CERT_PATH),
    };
}

// Returns authResult ("ok" if successful) and userId.
async function authenticate(
    authCodeBase64: string
): Promise<{ authResult: string, userId: string }> {
    // Decode auth
    const authData = decodeAuth(authCodeBase64);
    if (!authData) {
        return { authResult: 'failed to decode auth code', userId: '' };
    }
    const { userId, password } = authData;

    // Validate userId
    if (!validateUserId(userId)) {
        return { authResult: 'invalid user ID', userId: '' };
    }

    // Verify user
    const isVerified = await userService.verifyUser(userId, password);
    if (!isVerified) {
        return { authResult: 'user ID or password is incorrect', userId: '' };
    }

    return { authResult: 'ok', userId: userId };
}

// Request handler
async function handleRequest(
    req: IncomingMessage,
    res: ServerResponse
) {
    const url = new URL(req.url || '', `https://${req.headers.host}`);
    const pathname = url.pathname;

    // Handle /authenticate
    if (pathname === `${CONFIG.BINCALL_API_PATH}/authenticate`) {
        const authParam = url.searchParams.get('auth');

        if (!authParam) {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ authResult: 'missing auth parameter' }));
            return;
        }

        // Decode auth
        const authData = decodeAuth(authParam);
        if (!authData) {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ authResult: 'failed to decode auth' }));
            return;
        }

        const { userId, password } = authData;

        // Validate userId
        if (!validateUserId(userId)) {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ authResult: 'invalid user ID' }));
            return;
        }

        // Check if user exists
        const userExists = await userService.userExists(userId);

        if (userExists) {
            // Verify password
            const isVerified = await userService.verifyUser(userId, password);

            if (isVerified) {
                // Auth successful
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ authResult: 'ok' }));
            } else {
                // Password incorrect
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ authResult: 'incorrect password' }));
            }
        } else {
            // User doesn't exist - create new user
            if (!validatePassword(password)) {
                // Password validation failed
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ authResult: 'failed to create new user: bad password' }));
                return;
            }

            const newUser = await userService.createUser(userId, password);

            if (newUser) {
                // User created successfully
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ authResult: 'ok-created' }));
            } else {
                // User creation failed
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ authResult: 'failed to create new user' }));
            }
        }
        return;
    }

    // Handle /delete-acc
    if (pathname === `${CONFIG.BINCALL_API_PATH}/delete-acc`) {
        const authParam = url.searchParams.get('auth');

        if (!authParam) {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ authResult: 'no auth param' }));
            return;
        }

        const { authResult, userId } = await authenticate(authParam)
        if (authResult !== "ok") {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ authResult: authResult }));
            return;
        }

        let deleteResult = await userService.deleteUser(userId)
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
            authResult: authResult,
            deleteResult: deleteResult ? 'ok' : 'failed'
        }));
        return;
    }

    // Handle /dummy
    if (pathname === `${CONFIG.BINCALL_API_PATH}/dummy`) {
        const numBytes = 10 + Math.floor(Math.random() * 991); // 10 to 1000
        const randomBytes = Buffer.alloc(numBytes);

        // Fill buffer with random bytes using Math.random()
        for (let i = 0; i < numBytes; i++) {
            randomBytes[i] = Math.floor(Math.random() * 255.999);
        }

        res.writeHead(200, { 'Content-Type': 'application/octet-stream' });
        res.end(randomBytes);
        return;
    }

    // Handle /whos-calling
    if (pathname === `${CONFIG.BINCALL_API_PATH}/whos-calling`) {
        const authParam = url.searchParams.get('auth');

        if (!authParam) {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ authResult: 'no auth param' }));
            return;
        }

        const { authResult, userId } = await authenticate(authParam)
        if (authResult !== "ok") {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ authResult: authResult }));
            return;
        }

        // Clean up old calls
        await userService.cleanupOldCalls(userId);

        // Get incoming calls
        const incomingCalls = await userService.getIncomingCalls(userId);
        const sixtySecondsAgo = Date.now() - 60000;

        // Only the calls that are not older than 60 seconds and not answered
        // + remove the answered field.
        const recentCalls = incomingCalls
            .filter(call => call.timestamp > sixtySecondsAgo && !call.answered)
            .map(call => ({
                caller: call.caller,
                timestamp: call.timestamp,
            }));

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
            authResult: authResult,
            calls: recentCalls,
        }));
        return;
    }

    // Default: serve HTML page
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(defaultHtml);
}

const activeServers = new Array<http.Server | https.Server>()

// Run HTTP server if enabled
if (CONFIG.HTTP_PORT != null && CONFIG.HTTP_REJECT) {
    // Create HTTP server solely for rejecting non-secure connections
    const server = http.createServer((req: IncomingMessage, res: ServerResponse) => {
        res.writeHead(403, {
            'Content-Type': 'text/plain',
            'Connection': 'close'
        });
        res.end('Please use https:// instead of http://.');
        req.destroy();
    });
    server.listen(CONFIG.HTTP_PORT, () => {
        console.log(`HTTP server rejecting connections on port ${CONFIG.HTTP_PORT}`);
    });
} else if (CONFIG.HTTP_PORT != null) {
    // Create normally functioning HTTP server
    const server = http.createServer(handleRequest);
    server.listen(CONFIG.HTTP_PORT, () => {
        console.log(`HTTP server running on port ${CONFIG.HTTP_PORT}`);
    });
    activeServers.push(server)
} else {
    console.log(`HTTP server is disabled`);
}

// Run HTTPS server if enabled
if (CONFIG.HTTPS_PORT != null) {
    if (sslOptions == null) {
        throw Error(
            "SSL certificate and key paths are required for the HTTPS server"
        )
    }

    // Create HTTPS server
    const server = https.createServer(sslOptions, handleRequest);
    server.listen(CONFIG.HTTPS_PORT, () => {
        console.log(`HTTPS server running on port ${CONFIG.HTTPS_PORT}`);
    });
    activeServers.push(server)
} else {
    console.log(`HTTPS server is disabled`);
}

// Create WebSocket server(s)
activeServers.forEach(server => {
    const ws_server = new WebSocket.Server({ server });
    ws_server.on('connection', async (ws: WebSocket, req: IncomingMessage) => {
        try {
            ws.on('error', (error: Error) => {
                console.error('WebSocket error:', error);
                ws.terminate();
            });

            const url = new URL(req.url || '', `https://${req.headers.host}`);
            const pathname = url.pathname;

            // Handle WebSocket connections
            if (pathname === `${CONFIG.BINCALL_API_PATH}/call`) {
                await handleCallConnection(ws, req);
            } else if (pathname === `${CONFIG.BINCALL_API_PATH}/pickup`) {
                await handlePickupConnection(ws, req);
            } else {
                // Unknown WebSocket path
                ws.close(1008, 'Unknown path');
            }
        } catch (e) {
            console.error("Failed to handle WebSocket connection:", e)
        }
    });
});
