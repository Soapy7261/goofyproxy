import * as https from 'https';
import * as http from 'http';
import * as fs from 'fs';
import * as path from 'path';
import * as WebSocket from 'ws';
import { IncomingMessage, ServerResponse } from 'http';
import { URL } from 'url';
import { CONFIG } from './config';
import { decodeAuth } from './utils/auth';
import { validateUserId, validatePassword } from './utils/validation';
import { UserService } from './services/userService';
import { handleCallConnection, handlePickupConnection } from './routes/websocketHandler';

const userService = new UserService();

// Read SSL certificate and key
const sslOptions: https.ServerOptions = {
    key: fs.readFileSync(CONFIG.SSL_KEY_PATH),
    cert: fs.readFileSync(CONFIG.SSL_CERT_PATH),
};

// Read default HTML page
const defaultHtml = fs.readFileSync(CONFIG.DEFAULT_HTML_PATH, 'utf-8');

// Create HTTP server solely for rejecting non-secure connections
const httpServer = http.createServer((req: IncomingMessage, res: ServerResponse) => {
    // Forcefully reject all plain HTTP connections
    res.writeHead(403, {
        'Content-Type': 'text/plain',
        'Connection': 'close'
    });
    res.end('Please use https:// instead of http://.');

    // Immediately destroy the connection to be more aggressive
    req.destroy();
});

// returns authResult ("ok" if successful) and userId.
async function authenticate(auth_code_base64: string): Promise<{ authResult: string, userId: string }> {
    // Decode auth
    const authData = decodeAuth(auth_code_base64);
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

// Create HTTPS server
const server = https.createServer(sslOptions, async (req: IncomingMessage, res: ServerResponse) => {
    const url = new URL(req.url || '', `https://${req.headers.host}`);
    const pathname = url.pathname;

    // Handle /prepare
    if (pathname === `${CONFIG.WSIO_PATH_IN_REQUEST}/prepare`) {
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
                res.end(JSON.stringify({ authResult: 'failed to create new user because of invalid password' }));
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
    if (pathname === `${CONFIG.WSIO_PATH_IN_REQUEST}/delete-acc`) {
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
    if (pathname === `${CONFIG.WSIO_PATH_IN_REQUEST}/dummy`) {
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
    if (pathname === `${CONFIG.WSIO_PATH_IN_REQUEST}/whos-calling`) {
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
});

// Create WebSocket server
const wss = new WebSocket.Server({ server });

wss.on('connection', async (ws: WebSocket, req: IncomingMessage) => {
    const url = new URL(req.url || '', `https://${req.headers.host}`);
    const pathname = url.pathname;

    // Handle WebSocket connections
    if (pathname === `${CONFIG.WSIO_PATH_IN_REQUEST}/call`) {
        await handleCallConnection(ws, req);
    } else if (pathname === `${CONFIG.WSIO_PATH_IN_REQUEST}/pickup`) {
        await handlePickupConnection(ws, req);
    } else {
        // Unknown WebSocket path
        ws.close(1008, 'Unknown path');
    }
});

// Start HTTP server solely for rejecting non-secure connections
httpServer.listen(CONFIG.HTTP_PORT, () => {
    console.log(`HTTP server rejecting connections on port ${CONFIG.HTTP_PORT}`);
});

// Start server
server.listen(CONFIG.HTTPS_PORT, () => {
    console.log(`HTTPS server running on port ${CONFIG.HTTPS_PORT}`);
    console.log(`Users directory: ${path.resolve(CONFIG.USERS_DIR)}`);
    console.log(`Serving HTML from: ${path.resolve(CONFIG.DEFAULT_HTML_PATH)}`);
});
