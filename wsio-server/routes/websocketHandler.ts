import * as WebSocket from 'ws';
import { CONFIG } from '../config';
import { decodeAuth } from '../utils/auth';
import { validateUserId } from '../utils/validation';
import { UserService } from '../services/userService';
import { IncomingMessage } from 'http';
import { URL } from 'url';
import { Mutex, MutexInterface } from 'async-mutex';

const userService = new UserService();

class Call {
    user0: string
    user1: string
    timestamp: number

    user0Packets: Array<Buffer>
    user0PacketsMutex: Mutex
    user1Packets: Array<Buffer>
    user1PacketsMutex: Mutex

    constructor(user0: string, user1: string, timestamp: number) {
        if (user0 < user1) {
            this.user0 = user0
            this.user1 = user1
        } else {
            this.user0 = user1
            this.user1 = user0
        }
        this.timestamp = timestamp

        this.user0Packets = new Array<Buffer>()
        this.user0PacketsMutex = new Mutex()
        this.user1Packets = new Array<Buffer>()
        this.user1PacketsMutex = new Mutex()
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

    const newCall = new Call(user0, user1, timestamp)

    const release = await currentCallsMutex.acquire();

    let foundSameCall: Call | null = null;
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
            foundSameCall = call;
        }
    }

    if (foundSameCall !== null) {
        release()
        return foundSameCall
    } else {
        currentCalls.push(newCall)
        const addedCall = currentCalls[currentCalls.length - 1]
        release()
        return addedCall
    }
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

async function startReceiveLoop(
    ws: WebSocket,
    myId: string,
    theirId: string,
    theirPackets: Array<Buffer>,
    theirPakcetsMutex: Mutex
): Promise<void> {
    let isActive = true;
    const poll = async () => {
        while (isActive && ws.readyState === WebSocket.OPEN) {
            let release: MutexInterface.Releaser | null = null
            try {
                await new Promise(resolve => setTimeout(
                    resolve,
                    CONFIG.PACKET_POLL_INTERVAL_MS
                ));

                release = await theirPakcetsMutex.acquire();
                if (theirPackets.length < 1) {
                    release()
                    continue;
                }
                for (let i = 0; i < theirPackets.length; i++) {
                    const packet = theirPackets[i];

                    if (packet.byteLength < 1) {
                        // Empty packet means end of call

                        theirPackets.length = 0
                        release()

                        isActive = false;
                        await removeCall(myId, theirId)
                        ws.terminate()
                        return;
                    }

                    // Send binary frame
                    ws.send(packet);
                }
                theirPackets.length = 0
                release()
            } catch (error) {
                console.error(
                    `Error in receive loop (${myId} <- ${theirId}):`,
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

export async function handleCallConnection(
    ws: WebSocket,
    req: IncomingMessage
): Promise<void> {
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
    const callAdded = await userService.addIncomingCall(
        peerParam,
        userId,
        timestamp
    );
    if (!callAdded) {
        ws.send('peer-not-found');
        ws.close();
        return;
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
            ws.send('call-failed');
            ws.close();
            return;
        }
        if (ourCall.answered) {
            answered = true;
            break;
        }
        await new Promise(
            resolve => setTimeout(resolve, CONFIG.CALL_ANSWER_POLL_INTERVAL_MS)
        );
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
        ws.send('call-failed');
        ws.close();
        return;
    }

    // Add the call and inform the client if userId and peerParam are already in
    // a different call.
    const call = await addCall(userId, peerParam, timestamp)
    if (call === null) {
        ws.send('already-in-call')
        ws.close();
        return;
    }

    await startCall(ws, call, userId);
}

export async function handlePickupConnection(
    ws: WebSocket,
    req: IncomingMessage
): Promise<void> {
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

    // Get incoming call from peer
    const incomingCall = await userService.getIncomingCall(
        userId,
        peerParam,
        null,
        true,
        false
    );
    if (!incomingCall) {
        ws.send('no-call');
        ws.close();
        return;
    }
    const timestamp = incomingCall.timestamp;

    // Add the call and inform the client if userId and peerParam are already in
    // a different call.
    const call = await addCall(userId, peerParam, timestamp)
    if (call === null) {
        ws.send('already-in-call')
        ws.close();
        return;
    }

    // Answer the call
    const answered = await userService.answerCall(
        userId,
        peerParam,
        call.timestamp
    );
    if (!answered) {
        await removeCall(userId, peerParam)
        ws.send('no-call');
        ws.close();
        return;
    }

    await startCall(ws, call, userId);
}

async function startCall(ws: WebSocket, call: Call, myId: string) {
    // Make references to our and the other sides' packet array and mutex
    let myPackets = call.user0Packets
    let myPakcetsMutex = call.user0PacketsMutex
    let theirPackets = call.user1Packets
    let theirPakcetsMutex = call.user1PacketsMutex
    let theirId = call.user1
    if (myId === call.user1) {
        myPackets = call.user1Packets
        myPakcetsMutex = call.user1PacketsMutex
        theirPackets = call.user0Packets
        theirPakcetsMutex = call.user0PacketsMutex
        theirId = call.user0
    }

    // Send call-start
    ws.send('call-start');

    // Start receive loop
    startReceiveLoop(
        ws,
        myId,
        theirId,
        theirPackets,
        theirPakcetsMutex
    ).catch(console.error);


    // Handle incoming WebSocket messages
    ws.on('message', async (data: WebSocket.Data) => {
        if (data instanceof Buffer && data.byteLength > 0) {
            try {
                const release = await myPakcetsMutex.acquire();
                myPackets.push(data)
                release()
            } catch (error) {
                console.error('Failed to write packet:', error);
            }
        }
    });

    // Handle WebSocket close
    ws.on('close', async () => {
        try {
            // Push empty packet to indicate end of call
            const release = await myPakcetsMutex.acquire();
            myPackets.push(Buffer.alloc(0))
            release()

            // Remove call from current calls
            await removeCall(myId, theirId)
        } catch (error) {
            console.error('Failed to end the call:', error);
        }
    });
}
