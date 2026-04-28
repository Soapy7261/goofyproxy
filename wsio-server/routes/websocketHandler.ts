import * as WebSocket from 'ws';
import { CONFIG } from '../config';
import { decodeAuth } from '../utils/auth';
import { validateUserId } from '../utils/validation';
import { UserService } from '../services/userService';
import { CallService } from '../services/callService';
import { IncomingMessage } from 'http';
import { URL } from 'url';
import { Mutex } from 'async-mutex';

const userService = new UserService();

const currentCalls = Array<{ peers: string, timestamp: number }>()
const currentCallsMutex = new Mutex();

async function startReceiveLoop(
    ws: WebSocket,
    userId: string,
    peerId: string,
    timestamp: number
): Promise<void> {
    let inPacketIdx = 1;
    let isActive = true;

    const poll = async () => {
        while (isActive && ws.readyState === WebSocket.OPEN) {
            try {
                const packet = await CallService.readPacketFromPeer(
                    peerId,
                    userId,
                    timestamp,
                    inPacketIdx
                );

                if (packet !== null) {
                    inPacketIdx++;
                    if (packet.isClose) {
                        // Close the connection
                        isActive = false;
                        await removeCall(userId, peerId)
                        ws.terminate()
                        return;
                    }
                    else if (packet.data !== null) {
                        // Send binary frame
                        ws.send(packet.data);
                    }
                }
            } catch (error) {
                console.error('Error in receive loop:', error);
            }

            await new Promise(resolve => setTimeout(resolve, CONFIG.PACKET_POLL_INTERVAL_MS));
        }
    };

    await poll();
}

export async function handleCallConnection(ws: WebSocket, req: IncomingMessage): Promise<void> {
    const url = new URL(req.url || '', `https://${req.headers.host}`);
    const authParam = url.searchParams.get('auth');
    const peerParam = url.searchParams.get('peer');

    if (!authParam || !peerParam) {
        ws.send('auth-failed');
        ws.close();
        return;
    }

    // Decode auth
    const authData = decodeAuth(authParam);
    if (!authData) {
        ws.send('auth-failed');
        ws.close();
        return;
    }

    const { userId, password } = authData;

    // Validate userId
    if (!validateUserId(userId)) {
        ws.send('auth-failed');
        ws.close();
        return;
    }

    // Verify user exists and password is correct
    const isVerified = await userService.verifyUser(userId, password);
    if (!isVerified) {
        ws.send('auth-failed');
        ws.close();
        return;
    }

    // Validate peer
    if (peerParam === userId) {
        ws.send('peer-not-found');
        ws.close();
        return;
    }
    if (!validateUserId(peerParam)) {
        ws.send('peer-not-found');
        ws.close();
        return;
    }

    // Check if peer exists
    const peerExists = await userService.userExists(peerParam);
    if (!peerExists) {
        ws.send('peer-not-found');
        ws.close();
        return;
    }

    const timestamp = Date.now();

    // Add incoming call to peer
    const callAdded = await userService.addIncomingCall(peerParam, userId, timestamp);
    if (!callAdded) {
        ws.send('peer-not-found');
        ws.close();
        return;
    }

    // Poll for answer
    const startTime = Date.now();
    let answered = false;

    while (Date.now() - startTime < CONFIG.CALL_TIMEOUT_MS) {
        const peerCalls = await userService.getIncomingCalls(peerParam);
        const ourCall = peerCalls.find(
            call => call.caller === userId && call.timestamp === timestamp
        );

        if (!ourCall) {
            // Call was removed
            ws.send('call-failed');
            ws.close();
            return;
        }

        if (ourCall.answered) {
            answered = true;
            break;
        }

        await new Promise(resolve => setTimeout(resolve, CONFIG.SLOW_POLL_INTERVAL_MS));
    }

    if (!answered) {
        ws.send('call-failed');
        ws.close();
        return;
    }

    // Check if userId and peerParam are already in a different call
    if (!(await addCall(userId, peerParam, timestamp))) {
        ws.send('already-in-call')
        ws.close();
        return;
    }

    // Remove the incoming call
    await userService.removeIncomingCall(peerParam, userId, timestamp);

    // Delete old packets sent from userId to peerParam
    await CallService.cleanOutbox(userId, peerParam)

    // Create initial empty packet (packet 0)
    await CallService.writePacket(userId, peerParam, timestamp, 0, Buffer.alloc(0));

    // Send call-start
    ws.send('call-start');

    // Wait for peer's initial packet
    const packetReceived = await CallService.waitForInitialPacket(
        peerParam,
        userId,
        timestamp,
        CONFIG.CALL_TIMEOUT_MS
    );

    if (!packetReceived) {
        ws.close();
        return;
    }

    // Start receive loop
    startReceiveLoop(ws, userId, peerParam, timestamp).catch(console.error);

    // Handle incoming WebSocket messages
    let outPacketIdx = 1;
    ws.on('message', async (data: WebSocket.Data) => {
        if (data instanceof Buffer) {
            try {
                await CallService.writePacket(userId, peerParam, timestamp, outPacketIdx, data);

                // Update the inPacketIdx in the receive loop
                outPacketIdx++;
            } catch (error) {
                console.error('Error writing packet:', error);
            }
        }
    });

    // Handle WebSocket close
    ws.on('close', async () => {
        try {
            await CallService.writePacket(
                userId,
                peerParam,
                timestamp,
                outPacketIdx,
                Buffer.alloc(0),
                "-close"
            );
            await removeCall(userId, peerParam)
        } catch (error) {
            console.error('Error creating close packet:', error);
        }
    });
}

export async function handlePickupConnection(ws: WebSocket, req: IncomingMessage): Promise<void> {
    const url = new URL(req.url || '', `https://${req.headers.host}`);
    const authParam = url.searchParams.get('auth');
    const peerParam = url.searchParams.get('peer');

    if (!authParam || !peerParam) {
        ws.send('auth-failed');
        ws.close();
        return;
    }

    // Decode auth
    const authData = decodeAuth(authParam);
    if (!authData) {
        ws.send('auth-failed');
        ws.close();
        return;
    }

    const { userId, password } = authData;

    // Validate userId
    if (!validateUserId(userId)) {
        ws.send('auth-failed');
        ws.close();
        return;
    }

    // Verify user exists and password is correct
    const isVerified = await userService.verifyUser(userId, password);
    if (!isVerified) {
        ws.send('auth-failed');
        ws.close();
        return;
    }

    // Validate peer
    if (peerParam === userId) {
        ws.send('peer-not-found');
        ws.close();
        return;
    }
    if (!validateUserId(peerParam)) {
        ws.send('peer-not-found');
        ws.close();
        return;
    }

    // Check if peer exists
    const peerExists = await userService.userExists(peerParam);
    if (!peerExists) {
        ws.send('peer-not-found');
        ws.close();
        return;
    }

    // Get incoming calls and find the peer's call
    const incomingCalls = await userService.getIncomingCalls(userId);
    const call = incomingCalls.find(c => c.caller === peerParam && !c.answered);

    if (!call) {
        ws.send('no-call');
        ws.close();
        return;
    }

    // Check if userId and peerParam are already in a different call
    if (!(await addCall(userId, peerParam, call.timestamp))) {
        ws.send('already-in-call')
        ws.close();
        return;
    }

    // Answer the call
    const answered = await userService.answerCall(userId, peerParam, call.timestamp);
    if (!answered) {
        ws.send('no-call');
        ws.close();
        return;
    }

    const timestamp = call.timestamp;

    // Delete old packets sent from userId to peerParam
    await CallService.cleanOutbox(userId, peerParam)

    // Create initial empty packet (packet 0)
    await CallService.writePacket(userId, peerParam, timestamp, 0, Buffer.alloc(0));

    // Send call-start
    ws.send('call-start');

    // Wait for caller's initial packet
    const packetReceived = await CallService.waitForInitialPacket(
        peerParam,
        userId,
        timestamp,
        CONFIG.CALL_TIMEOUT_MS
    );

    if (!packetReceived) {
        ws.close();
        return;
    }

    // Start receive loop
    startReceiveLoop(ws, userId, peerParam, timestamp).catch(console.error);

    // Handle incoming WebSocket messages
    let outPacketIdx = 1;
    ws.on('message', async (data: WebSocket.Data) => {
        if (data instanceof Buffer) {
            try {
                await CallService.writePacket(userId, peerParam, timestamp, outPacketIdx, data);
                outPacketIdx++;
            } catch (error) {
                console.error('Error writing packet:', error);
            }
        }
    });

    // Handle WebSocket close
    ws.on('close', async () => {
        try {
            await CallService.writePacket(
                userId,
                peerParam,
                timestamp,
                outPacketIdx,
                Buffer.alloc(0),
                "-close"
            );
            await removeCall(userId, peerParam)
        } catch (error) {
            console.error('Error creating close packet:', error);
        }
    });
}

async function addCall(
    user1: string,
    user2: string,
    timestamp: number
): Promise<boolean> {
    const combined = (user1 < user2)
        ? `${user1} : ${user2}`
        : `${user2} : ${user1}`

    const release = await currentCallsMutex.acquire();

    let foundSameCall = false;
    for (let i = 0; i < currentCalls.length; i++) {
        const call = currentCalls[i];
        if (call.peers !== combined) {
            continue;
        }
        if (call.timestamp !== timestamp) {
            release();
            return false;
        } else {
            foundSameCall = true;
        }
    }

    if (foundSameCall) {
        release()
        return true
    } else {
        currentCalls.push({ peers: combined, timestamp: timestamp })
        release()
        return true
    }
}

async function removeCall(user1: string, user2: string) {
    const combined = (user1 < user2)
        ? `${user1} : ${user2}`
        : `${user2} : ${user1}`

    const release = await currentCallsMutex.acquire()
    for (let i = 0; i < currentCalls.length; i++) {
        if (currentCalls[i].peers == combined) {
            currentCalls.splice(i, 1)
            i--;
        }
    }
    release()
}
