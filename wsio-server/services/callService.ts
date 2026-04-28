import * as fs from 'fs/promises';
import { constants } from 'fs';
import * as path from 'path';
import { CONFIG } from '../config';

export class CallService {
    static getCallOutboxDir(userDir: string): string {
        return path.join(userDir, 'call-outbox');
    }

    static getPacketFileName(peerId: string, timestamp: number, packetIndex: number): string {
        return `${peerId}@${timestamp}-${packetIndex}`;
    }

    static async ensureCallOutboxDir(userDir: string): Promise<string> {
        const outboxDir = this.getCallOutboxDir(userDir);
        await fs.mkdir(outboxDir, { recursive: true });
        return outboxDir;
    }

    // delete packets in userId's outbox sent to peerId 
    static async cleanOutbox(
        userId: string,
        peerId: string
    ): Promise<void> {
        const userDir = path.join(CONFIG.USERS_DIR, userId);
        const outboxDir = this.getCallOutboxDir(userDir);
        if (!(await pathExists(outboxDir))) {
            return
        }

        try {
            const files = await fs.readdir(outboxDir);
            const prefix = `${peerId}@`;

            for (const file of files) {
                if (!file.startsWith(prefix)) {
                    continue;
                }
                const filePath = path.join(outboxDir, file);
                await fs.unlink(filePath).catch(() => { });
            }
        } catch { }
    }

    static async writePacket(
        userId: string,
        peerId: string,
        timestamp: number,
        packetIndex: number,
        data: Buffer,
        filenameSuffix: string = ""
    ): Promise<string> {
        const userDir = path.join(CONFIG.USERS_DIR, userId);
        const outboxDir = await this.ensureCallOutboxDir(userDir);
        const fileName = this.getPacketFileName(peerId, timestamp, packetIndex);
        const filePath = path.join(outboxDir, fileName + filenameSuffix);
        await fs.writeFile(filePath, data);
        return filePath;
    }

    static async readPacketFromPeer(
        peerId: string,
        userId: string,
        timestamp: number,
        inPacketIdx: number
    ): Promise<{ index: number; data: Buffer | null; isClose: boolean } | null> {
        const peerDir = path.join(CONFIG.USERS_DIR, peerId);
        const peerOutboxDir = this.getCallOutboxDir(peerDir);

        let result: { index: number; data: Buffer | null; isClose: boolean } | null = null

        try {
            const files = await fs.readdir(peerOutboxDir);
            const prefix = `${userId}@${timestamp}-`;

            for (const file of files) {
                if (!file.startsWith(prefix)) {
                    continue;
                }

                let suffix = file.substring(prefix.length);

                let isClose = false
                if (suffix.endsWith("-close")) {
                    suffix = suffix.substring(0, suffix.length - 6)
                    isClose = true
                }

                const packetIndex = parseInt(suffix, 10);
                if (isNaN(packetIndex)) {
                    console.warn(`failed to parse packet index from filename "${file}"`)
                    continue;
                }

                const filePath = path.join(peerOutboxDir, file);

                if (packetIndex < inPacketIdx) {
                    // Delete old packets
                    await fs.unlink(filePath).catch(() => { });
                } else if (packetIndex === inPacketIdx && !isClose) {
                    // Read packet
                    try {
                        const data = await fs.readFile(filePath);

                        // Delete after reading
                        await fs.unlink(filePath).catch(() => { });

                        result = { index: packetIndex, data, isClose }
                    } catch {
                        // File might have been deleted already
                    }
                } else if (packetIndex === inPacketIdx && isClose) {
                    await fs.unlink(filePath).catch(() => { });
                    result = { index: inPacketIdx, data: null, isClose }
                }
            }
        } catch {
            // Outbox directory might not exist yet
        }

        return result;
    }

    static async waitForInitialPacket(
        peerId: string,
        userId: string,
        timestamp: number,
        timeoutMs: number
    ): Promise<boolean> {
        const startTime = Date.now();
        const peerDir = path.join(CONFIG.USERS_DIR, peerId);
        const peerOutboxDir = this.getCallOutboxDir(peerDir);
        const expectedFile = `${userId}@${timestamp}-0`;

        while (Date.now() - startTime < timeoutMs) {
            try {
                const files = await fs.readdir(peerOutboxDir);
                if (files.includes(expectedFile)) {
                    return true;
                }
            } catch {
                // Directory might not exist yet
            }

            await new Promise(resolve => setTimeout(resolve, CONFIG.SLOW_POLL_INTERVAL_MS));
        }

        return false;
    }
}

async function pathExists(path: string): Promise<boolean> {
    try {
        await fs.access(path, constants.F_OK);
        return true;
    } catch {
        return false;
    }
}

async function pathIsFile(path: string): Promise<boolean> {
    try {
        await fs.access(path, constants.F_OK);
        const stats = await fs.stat(path);
        return stats.isFile();
    } catch {
        return false;
    }
}

async function pathIsDirectory(path: string): Promise<boolean> {
    try {
        await fs.access(path, constants.F_OK);
        const stats = await fs.stat(path);
        return stats.isDirectory();
    } catch {
        return false;
    }
}