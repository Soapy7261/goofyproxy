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
import { insecureEncrypt, insecureDecrypt } from './utils/crypto';
import { Mutex, MutexInterface } from 'async-mutex';

const userService = new UserService();
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

class Call {
    id: number;
    user0: string;
    user1: string;
    timestamp: number;

    user0Packets: Array<Buffer>;
    user0PacketsMutex: Mutex;
    user1Packets: Array<Buffer>;
    user1PacketsMutex: Mutex;

    user0LastActivity: number;
    user1LastActivity: number;
    endedAfterEmptyPacket: boolean;

    user0Key: string = "";
    user1Key: string = "";

    constructor(user0: string, user1: string, timestamp: number) {
        if (user0 < user1) {
            this.user0 = user0
            this.user1 = user1
        } else {
            this.user0 = user1
            this.user1 = user0
        }
        this.timestamp = timestamp;
        this.id = hashFNV1a(this.user0, this.user1, this.timestamp);

        this.user0Packets = new Array<Buffer>();
        this.user0PacketsMutex = new Mutex();
        this.user1Packets = new Array<Buffer>();
        this.user1PacketsMutex = new Mutex();

        this.user0LastActivity = Date.now();
        this.user1LastActivity = Date.now();
        this.endedAfterEmptyPacket = false;
    }
}

const currentCalls = new Array<Call>()
const currentCallsMutex = new Mutex();

async function addCall(
    user0_unsorted: string,
    user1_unsorted: string,
    timestamp: number
): Promise<Call | null> {
    let user0 = user0_unsorted
    let user1 = user1_unsorted
    if (user1_unsorted < user0_unsorted) {
        user0 = user1_unsorted
        user1 = user0_unsorted
    }

    const release = await currentCallsMutex.acquire();

    for (let i = 0; i < currentCalls.length; i++) {
        const call = currentCalls[i];
        if (call.user0 !== user0 || call.user1 !== user1) {
            continue;
        }

        if (call.timestamp !== timestamp) {
            // found a call with the same peers but a different timestamp, can't
            // add another one!
            release();
            return null;
        } else {
            release();
            return call;
        }
    }

    currentCalls.push(new Call(user0, user1, timestamp));
    const addedCall = currentCalls[currentCalls.length - 1];
    release();
    return addedCall;
}

async function removeCall(user0_unsorted: string, user1_unsorted: string) {
    let user0 = user0_unsorted
    let user1 = user1_unsorted
    if (user1_unsorted < user0_unsorted) {
        user0 = user1_unsorted
        user1 = user0_unsorted
    }

    const release = await currentCallsMutex.acquire()
    for (let i = 0; i < currentCalls.length; i++) {
        const call = currentCalls[i];
        if (call.user0 !== user0 || call.user1 !== user1) {
            continue;
        }
        currentCalls.splice(i, 1)
        i--;
    }
    release()
}

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

        if (authParam == null) {
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
        if (authParam == null) {
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

        const release = await currentCallsMutex.acquire();
        for (let i = 0; i < currentCalls.length; i++) {
            const call = currentCalls[i];
            let foundUser = false;
            if (call.user0 === userId) {
                foundUser = true;
                const release = await call.user0PacketsMutex.acquire();
                call.user0Packets.push(Buffer.alloc(0));
                release();
            }
            if (call.user1 === userId) {
                foundUser = true;
                const release = await call.user1PacketsMutex.acquire();
                call.user1Packets.push(Buffer.alloc(0));
                release();
            }

            if (!foundUser) {
                continue;
            }
            currentCalls.splice(i, 1);
            i--;
        }
        release();

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

        res.writeHead(
            200,
            {
                'Content-Type': 'application/octet-stream',
                'Content-Length': numBytes
            }
        );
        res.end(randomBytes);
        return;
    }

    // Handle /whos-calling
    if (pathname === `${CONFIG.BINCALL_API_PATH}/whos-calling`) {
        const authParam = url.searchParams.get('auth');
        if (authParam == null) {
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

    // Handle /connection-modes
    if (pathname === `${CONFIG.BINCALL_API_PATH}/connection-modes`) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
            connectionModes: ['websocket', 'http']
        }));
        return;
    }

    // Handle /call-http
    const isCallRequest = pathname === `${CONFIG.BINCALL_API_PATH}/call-http`;
    const isPickupRequest = pathname === `${CONFIG.BINCALL_API_PATH}/pickup-http`;
    if (isCallRequest || isPickupRequest) {
        res.writeHead(200, { 'Content-Type': 'application/json' });

        const authParam = url.searchParams.get('auth');
        const peerParam = url.searchParams.get('peer');

        let result: Array<string | Call> | null = null;
        if (isCallRequest) {
            makeCall(authParam, peerParam)
                .then(v => { result = v; })
                .catch(reason => {
                    console.error('makeCall():', reason);
                    result = ['no-result'];
                });
            while (result == null) {
                res.write('~')
                await sleep(CONFIG.CALL_ANSWER_POLL_INTERVAL_MS);
            }
        } else {
            try {
                result = await pickup(authParam, peerParam);
            } catch (error) {
                console.error('pickup():', error);
                result = ['no-result'];
            }
        }

        if (result.length < 1) {
            res.end(JSON.stringify({
                result: 'no-result'
            }));
            return;
        }

        const message = result[0] as string;
        res.end(JSON.stringify({
            result: message
        }));

        if (!message.startsWith("call-start#")) {
            return;
        }

        let call = result[1] as Call;
        let userId = result[3] as string;
        while (true) {
            await sleep(10_000);
            if (call.endedAfterEmptyPacket) {
                break;
            }

            const release = await currentCallsMutex.acquire();
            if (currentCalls.filter(
                (v, idx, arr) => v.id === call.id
            ).length < 1) {
                release();
                break;
            }
            release();

            if (call.user0 === userId && Date.now() - call.user0LastActivity > 30_000) {
                const release = await call.user0PacketsMutex.acquire();
                call.user0Packets.push(Buffer.alloc(0));
                release();
                break;
            } else if (call.user1 === userId && Date.now() - call.user1LastActivity > 30_000) {
                const release = await call.user1PacketsMutex.acquire();
                call.user1Packets.push(Buffer.alloc(0));
                release();
                break;
            }
        }
        return;
    }

    // Handle /http-chunk
    if (pathname === `${CONFIG.BINCALL_API_PATH}/http-chunk`) {
        const authParam = url.searchParams.get('auth');
        const peerParam = url.searchParams.get('peer');
        const callIdParam = url.searchParams.get('call-id');

        if (authParam == null || peerParam == null || callIdParam == null) {
            writeHttpChunkResponse(res, 'missing parameters');
            return;
        }

        const { authResult, userId } = await authenticate(authParam)
        if (authResult !== "ok") {
            writeHttpChunkResponse(res, `${authResult}`);
            return;
        }

        let callId: number;
        try {
            callId = parseInt(callIdParam);
        } catch (error) {
            writeHttpChunkResponse(res, 'call-id must be a valid integer');
            return;
        }

        const release2 = await currentCallsMutex.acquire();
        const matchedCalls = currentCalls.filter(c => c.id === callId);
        release2();
        if (matchedCalls.length < 1) {
            writeHttpChunkResponse(res, 'end');
            return;
        }
        if (matchedCalls.length > 1) {
            writeHttpChunkResponse(res, 'duplicated call ID');
            return;
        }
        const call = matchedCalls[0];

        // Make references to our and the other sides' packet array and mutex
        let callKey = call.user0Key;
        let myPackets = call.user0Packets;
        let myPakcetsMutex = call.user0PacketsMutex;
        let theirPackets = call.user1Packets;
        let theirPakcetsMutex = call.user1PacketsMutex;
        let theirId = call.user1;
        if (userId === call.user1 && peerParam === call.user0) {
            callKey = call.user1Key;
            myPackets = call.user1Packets;
            myPakcetsMutex = call.user1PacketsMutex;
            theirPackets = call.user0Packets;
            theirPakcetsMutex = call.user0PacketsMutex;
            theirId = call.user0;

            call.user1LastActivity = Date.now();
        } else if (userId === call.user0 && peerParam === call.user1) {
            call.user0LastActivity = Date.now();
        } else {
            writeHttpChunkResponse(res, 'invalid user ID or peer ID');
            return;
        }

        if (call.endedAfterEmptyPacket) {
            writeHttpChunkResponse(res, 'end');
            return;
        }

        let endIt = false;

        // relay from client to peer
        try {
            if (req.headers['content-length'] != null
                && parseInt(req.headers['content-length']) > 0) {
                const reqBody = await readRequestBodyAsBuffer(req);
                if (reqBody.byteLength > 0) {
                    const release = await myPakcetsMutex.acquire();
                    myPackets.push(insecureDecrypt(reqBody, callKey));
                    release();
                }
            }
        } catch (error) {
            console.error(`Failed to write packet in http-chunk (${userId} -> ${theirId}):`, error);
        }

        if (url.searchParams.get('end') === '1') {
            endIt = true;
            const release = await myPakcetsMutex.acquire();
            myPackets.push(Buffer.alloc(0))
            release();
        }

        // relay from peer to client
        let peerData: Buffer = Buffer.alloc(0);
        let release: MutexInterface.Releaser | null = null;
        try {
            release = await theirPakcetsMutex.acquire();
            if (theirPackets.length > 0 || call.endedAfterEmptyPacket) {
                if (call.endedAfterEmptyPacket) {
                    endIt = true;
                } else {
                    for (let i = 0; i < theirPackets.length; i++) {
                        const packet = theirPackets[i];
                        if (packet.byteLength < 1) {
                            // Empty packet means end of call
                            endIt = true;
                            call.endedAfterEmptyPacket = true;
                            break;
                        }
                        peerData = Buffer.concat([peerData, packet]);
                    }
                }
                theirPackets.length = 0;
            }
            release()
        } catch (error) {
            endIt = true;
            if (release !== null) {
                try {
                    release()
                } catch { }
            }
            console.error(
                `Failed to receive packet in http-chunk (${userId} <- ${theirId}):`,
                error
            );
        }
        if (peerData.byteLength > 0) {
            peerData = insecureEncrypt(peerData, callKey);
        }

        if (endIt) {
            await removeCall(userId, theirId);
        }

        writeHttpChunkResponse(
            res,
            endIt ? 'end' : 'ok',
            peerData
        );
        return;
    }

    // Default: serve HTML page
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(defaultHtml);
}

function writeHttpChunkResponse(
    res: ServerResponse,
    status: string,
    data: Buffer | null = null
) {
    const statusBytes = Buffer.from(status);
    if (statusBytes.byteLength > 65535) {
        throw new Error("status message too long");
    }

    const part0 = Buffer.alloc(2);
    part0.writeUInt16BE(statusBytes.byteLength, 0);

    const part1 = statusBytes;

    const dataResolved = (data == null) ? Buffer.alloc(0) : data;
    const part2 = Buffer.alloc(4);
    part2.writeUInt32BE(dataResolved.byteLength);

    const part3 = dataResolved;

    const combined = Buffer.concat([part0, part1, part2, part3]);

    res.writeHead(200, {
        'Content-Type': 'application/octet-stream',
        'Content-Length': combined.byteLength
    });
    res.end(combined);
}

async function makeCall(
    authParam: string | null,
    peerParam: string | null
): Promise<Array<string | Call>> {
    if (authParam == null || peerParam == null) {
        return ['auth-failed'];
    }

    // Decode auth
    const authData = decodeAuth(authParam);
    if (!authData) {
        return ['auth-failed'];
    }

    const { userId, password } = authData;

    // Validate userId
    if (!validateUserId(userId)) {
        return ['auth-failed'];
    }

    // Verify user exists and password is correct
    const isVerified = await userService.verifyUser(userId, password);
    if (!isVerified) {
        return ['auth-failed'];
    }

    // Validate peer
    if (peerParam === userId) {
        return ['peer-not-found'];
    }
    if (!validateUserId(peerParam)) {
        return ['peer-not-found'];
    }

    // Check if peer exists
    const peerExists = await userService.userExists(peerParam);
    if (!peerExists) {
        return ['peer-not-found'];
    }

    const timestamp = Date.now();

    // Add incoming call to peer
    const callAdded = await userService.addIncomingCall(
        peerParam,
        userId,
        timestamp
    );
    if (!callAdded) {
        return ['peer-not-found'];
    }

    // Poll for answer
    const startTime = Date.now();
    let answered = false;
    while (Date.now() - startTime < CONFIG.CALL_TIMEOUT_MS) {
        const ourCall = await userService.getIncomingCall(
            peerParam,
            userId,
            timestamp,
            false,
            false
        );
        if (!ourCall) {
            // Call was removed
            return ['call-failed'];
        }
        if (ourCall.answered) {
            answered = true;
            break;
        }
        await sleep(CONFIG.CALL_ANSWER_POLL_INTERVAL_MS)
    }

    // Remove the incoming call
    await userService.getIncomingCall(
        peerParam,
        userId,
        timestamp,
        false,
        true
    );

    if (!answered) {
        return ['call-failed'];
    }

    // Add the call and inform the client if userId and peerParam are already in
    // a different call.
    const call = await addCall(userId, peerParam, timestamp)
    if (call === null) {
        return ['already-in-call-with-peer'];
    }

    // call-start
    const callKey = generateCallKey();
    if (userId === call.user0) {
        call.user0Key = callKey;
    } else {
        call.user1Key = callKey;
    }
    return [`call-start#${call.id}#${callKey}`, call, callKey, userId];
}

async function pickup(
    authParam: string | null,
    peerParam: string | null
): Promise<Array<string | Call>> {
    if (authParam == null || peerParam == null) {
        return ['auth-failed'];
    }

    // Decode auth
    const authData = decodeAuth(authParam);
    if (!authData) {
        return ['auth-failed'];
    }

    const { userId, password } = authData;

    // Validate userId
    if (!validateUserId(userId)) {
        return ['auth-failed'];
    }

    // Verify user exists and password is correct
    const isVerified = await userService.verifyUser(userId, password);
    if (!isVerified) {
        return ['auth-failed'];
    }

    // Validate peer
    if (peerParam === userId) {
        return ['peer-not-found'];
    }
    if (!validateUserId(peerParam)) {
        return ['peer-not-found'];
    }

    // Check if peer exists
    const peerExists = await userService.userExists(peerParam);
    if (!peerExists) {
        return ['peer-not-found'];
    }

    // Get incoming call from peer
    const incomingCall = await userService.getIncomingCall(
        userId,
        peerParam,
        null,
        true,
        false
    );
    if (!incomingCall) {
        return ['no-call'];
    }
    incomingCall.answered = true;
    const timestamp = incomingCall.timestamp;

    // Add the call and inform the client if userId and peerParam are already in
    // a different call.
    const call = await addCall(userId, peerParam, timestamp)
    if (call === null) {
        return ['already-in-call-with-peer'];
    }

    // Answer the call
    const answered = await userService.answerCall(
        userId,
        peerParam,
        call.timestamp
    );
    if (!answered) {
        await removeCall(userId, peerParam)
        return ['no-call'];
    }

    // call-start
    const callKey = generateCallKey();
    if (userId === call.user0) {
        call.user0Key = callKey;
    } else {
        call.user1Key = callKey;
    }
    return [`call-start#${call.id}#${callKey}`, call, callKey, userId];
}

async function startWsCall(ws: WebSocket, call: Call, callKey: string, myId: string) {
    // Make references to our and the other sides' packet array and mutex
    let myPackets = call.user0Packets;
    let myPakcetsMutex = call.user0PacketsMutex;
    let theirPackets = call.user1Packets;
    let theirPakcetsMutex = call.user1PacketsMutex;
    let theirId = call.user1;
    if (myId === call.user1) {
        myPackets = call.user1Packets;
        myPakcetsMutex = call.user1PacketsMutex;
        theirPackets = call.user0Packets;
        theirPakcetsMutex = call.user0PacketsMutex;
        theirId = call.user0;
    }

    // Start receive loop
    wsCallReceiveLoop(
        ws,
        callKey,
        myId,
        theirId,
        theirPackets,
        theirPakcetsMutex,
        call
    ).catch(console.error);


    // Handle incoming WebSocket messages
    ws.on('message', async (data: WebSocket.Data) => {
        if (data instanceof Buffer && data.byteLength > 0) {
            try {
                if (call.endedAfterEmptyPacket) {
                    return;
                }

                const release = await myPakcetsMutex.acquire();
                myPackets.push(insecureDecrypt(data, callKey))
                release()
            } catch (error) {
                console.error(`Failed to write packet (WS) (${myId} -> ${theirId}):`, error);
            }
        }
    });

    // Handle WebSocket close
    ws.on('close', async () => {
        try {
            // Push empty packet to indicate end of call
            try {
                const release = await myPakcetsMutex.acquire();
                myPackets.push(Buffer.alloc(0))
                release()
            } catch { }

            // Remove call from current calls
            await removeCall(myId, theirId)
        } catch (error) {
            console.error(`Failed to end the call (${myId} -> ${theirId}):`, error);
        }
    });
}

async function wsCallReceiveLoop(
    ws: WebSocket,
    key: string,
    myId: string,
    theirId: string,
    theirPackets: Array<Buffer>,
    theirPakcetsMutex: Mutex,
    call: Call
): Promise<void> {
    let isActive = true;
    const poll = async () => {
        while (isActive && ws.readyState === WebSocket.OPEN) {
            let release: MutexInterface.Releaser | null = null
            try {
                await sleep(CONFIG.PACKET_POLL_INTERVAL_MS)
                release = await theirPakcetsMutex.acquire();

                if (theirPackets.length < 1 && !call.endedAfterEmptyPacket) {
                    release()
                    continue;
                }

                let endIt = false;
                if (call.endedAfterEmptyPacket) {
                    endIt = true;
                } else {
                    let data = Buffer.alloc(0);
                    for (let i = 0; i < theirPackets.length; i++) {
                        const packet = theirPackets[i];
                        if (packet.byteLength < 1) {
                            // Empty packet means end of call
                            endIt = true;
                            break;
                        }
                        data = Buffer.concat([data, packet])
                    }

                    // Send binary frame
                    if (data.length > 0) {
                        ws.send(insecureEncrypt(data, key));
                    }
                }
                theirPackets.length = 0;

                if (endIt) {
                    call.endedAfterEmptyPacket = true;
                    isActive = false;
                    await removeCall(myId, theirId);
                    ws.terminate();
                }

                release()
            } catch (error) {
                console.error(
                    `Error in WebSocket call receive loop (${myId} <- ${theirId}):`,
                    error
                );

                if (release !== null) {
                    try {
                        release()
                    } catch { }
                }

                isActive = false;
                try {
                    await removeCall(myId, theirId)
                } catch { }
                try {
                    ws.terminate()
                } catch { }
                return;
            }
        }
    };

    await poll();
}

function getRandomElement<T>(arr: T[]): T {
    return arr[Math.floor(Math.random() * arr.length)];
}

function hashFNV1a(str1: string, str2: string, num: number): number {
    const FNV_OFFSET_BASIS = 2166136261;
    const FNV_PRIME = 16777619;

    let hash = FNV_OFFSET_BASIS;

    // Hash first string
    for (let i = 0; i < str1.length; i++) {
        hash = (hash ^ str1.charCodeAt(i)) * FNV_PRIME;
        hash >>>= 0; // Keep as unsigned 32-bit
    }

    // Hash second string
    for (let i = 0; i < str2.length; i++) {
        hash = (hash ^ str2.charCodeAt(i)) * FNV_PRIME;
        hash >>>= 0;
    }

    // Hash number (convert to bytes)
    const numBytes = new Uint8Array(new Uint32Array([num]).buffer);
    for (let i = 0; i < numBytes.length; i++) {
        hash = (hash ^ numBytes[i]) * FNV_PRIME;
        hash >>>= 0;
    }

    return hash;
}

function generateCallKey(): string {
    const keyParts0: string[] = ['pretty', 'beautiful', 'cute', 'angry', 'aggressive', 'wild', 'sad', 'happy', 'weird', 'surprised', 'excited', 'baby', 'old', 'grateful', 'tired', 'exhausted', 'hungry'];
    const keyParts1: string[] = ['panda', 'flamingo', 'duck', 'chicken', 'penguin', 'ostrich', 'bird', 'parrot', 'sheep', 'cow', 'goat', 'camel', 'fish', 'shark', 'whale', 'turtle', 'cat', 'dog', 'dolphin'];

    const keyPart0 = getRandomElement(keyParts0);
    const keyPart1 = getRandomElement(keyParts1);
    const keyPart2 = 100 + Math.floor(Math.random() * 9900);
    return `${keyPart0}-${keyPart1}-${keyPart2}`;
}

function readRequestBodyAsBuffer(req: IncomingMessage): Promise<Buffer> {
    return new Promise((resolve, reject) => {
        const chunks: Buffer[] = [];

        req.on('data', (chunk: Buffer) => {
            chunks.push(chunk);
        });

        req.on('end', () => {
            resolve(Buffer.concat(chunks));
        });

        req.on('error', (err) => {
            reject(err);
        });
    });
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
                const result = await makeCall(
                    url.searchParams.get('auth'),
                    url.searchParams.get('peer')
                );
                if (result.length < 1) {
                    ws.send('no-result');
                    ws.close();
                    return;
                }

                const message = result[0] as string;
                ws.send(message);
                if (!message.startsWith("call-start#")) {
                    ws.close();
                    return;
                }

                await startWsCall(
                    ws,
                    result[1] as Call,
                    result[2] as string,
                    result[3] as string
                );
            } else if (pathname === `${CONFIG.BINCALL_API_PATH}/pickup`) {
                const result = await pickup(
                    url.searchParams.get('auth'),
                    url.searchParams.get('peer')
                );
                if (result.length < 1) {
                    ws.send('no-result');
                    ws.close();
                    return;
                }

                const message = result[0] as string;
                ws.send(message);
                if (!message.startsWith("call-start#")) {
                    ws.close();
                    return;
                }

                await startWsCall(
                    ws,
                    result[1] as Call,
                    result[2] as string,
                    result[3] as string
                );
            } else {
                // Unknown WebSocket path
                ws.close(1008, 'Unknown path');
            }
        } catch (e) {
            console.error("Failed to handle WebSocket connection:", e)
        }
    });
});
