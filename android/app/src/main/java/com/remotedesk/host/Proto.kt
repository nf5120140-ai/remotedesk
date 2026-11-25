package com.remotedesk.host

import android.content.Context
import android.os.Build
import org.json.JSONObject
import java.math.BigInteger
import java.nio.ByteBuffer
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECPoint
import java.security.spec.ECPublicKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** Crypto + protocol helpers. Must stay byte-for-byte compatible with client/core.py. */
object Crypto {
    private val rnd = SecureRandom()

    fun hex(b: ByteArray): String {
        val sb = StringBuilder(b.size * 2)
        for (x in b) sb.append(String.format("%02x", x.toInt() and 0xff))
        return sb.toString()
    }

    fun unhex(s: String): ByteArray {
        require(s.length % 2 == 0)
        return ByteArray(s.length / 2) { i -> s.substring(i * 2, i * 2 + 2).toInt(16).toByte() }
    }

    fun randBytes(n: Int): ByteArray = ByteArray(n).also { rnd.nextBytes(it) }
    fun randHex(n: Int): String = hex(randBytes(n))

    fun hmac(key: ByteArray, data: ByteArray): ByteArray {
        val m = Mac.getInstance("HmacSHA256")
        m.init(SecretKeySpec(key, "HmacSHA256"))
        return m.doFinal(data)
    }

    fun sha256(vararg parts: ByteArray): ByteArray {
        val d = MessageDigest.getInstance("SHA-256")
        for (p in parts) d.update(p)
        return d.digest()
    }

    /** PBKDF2-HMAC-SHA256, 32 bytes (password encoded as UTF-8, like Python). */
    fun pbkdf2(password: String, salt: ByteArray, rounds: Int): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(password.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        mac.update(salt)
        mac.update(byteArrayOf(0, 0, 0, 1))
        var u = mac.doFinal()
        val out = u.copyOf()
        for (i in 1 until rounds) {
            u = mac.doFinal(u)
            for (j in out.indices) out[j] = (out[j].toInt() xor u[j].toInt()).toByte()
        }
        return out
    }

    fun deriveKey(password: String, hostId: String): ByteArray =
        pbkdf2(password, "remotedesk:$hostId".toByteArray(), 200_000)

    fun proof(key: ByteArray, label: String, vararg parts: String): String =
        hex(hmac(key, (label + "|" + parts.joinToString("|")).toByteArray()))

    fun sessionKey(key: ByteArray, hn: String, vn: String): ByteArray =
        hmac(key, "session|$hn|$vn".toByteArray())

    fun equalsConstTime(a: String, b: String): Boolean =
        MessageDigest.isEqual(a.toByteArray(), b.toByteArray())

    // ---------------- ECDH P-256 (approval requests)
    fun ecdhKeyPair(): KeyPair {
        val g = KeyPairGenerator.getInstance("EC")
        g.initialize(ECGenParameterSpec("secp256r1"))
        return g.generateKeyPair()
    }

    private fun fixed32(b: BigInteger): ByteArray {
        val a = b.toByteArray()
        return when {
            a.size == 32 -> a
            a.size > 32 -> a.copyOfRange(a.size - 32, a.size)
            else -> ByteArray(32 - a.size) + a
        }
    }

    fun encodePub(kp: KeyPair): ByteArray {
        val w = (kp.public as ECPublicKey).w
        return byteArrayOf(4) + fixed32(w.affineX) + fixed32(w.affineY)
    }

    fun ecdhKey(kp: KeyPair, peerHex: String, hn: String, vn: String): ByteArray {
        val p = unhex(peerHex)
        require(p.size == 65 && p[0] == 4.toByte())
        val x = BigInteger(1, p.copyOfRange(1, 33))
        val y = BigInteger(1, p.copyOfRange(33, 65))
        val params = (kp.public as ECPublicKey).params
        val peer = KeyFactory.getInstance("EC").generatePublic(ECPublicKeySpec(ECPoint(x, y), params))
        val ka = KeyAgreement.getInstance("ECDH")
        ka.init(kp.private)
        ka.doPhase(peer, true)
        val shared = ka.generateSecret()
        return sha256("remotedesk-ecdh|".toByteArray(), shared, "|$hn|$vn".toByteArray())
    }

    fun verificationCode(key: ByteArray): String {
        val d = sha256("code|".toByteArray(), key)
        val v = ((d[0].toLong() and 0xff) shl 24) or ((d[1].toLong() and 0xff) shl 16) or
                ((d[2].toLong() and 0xff) shl 8) or (d[3].toLong() and 0xff)
        val s = (v % 1_000_000).toString().padStart(6, '0')
        return s.substring(0, 3) + " " + s.substring(3)
    }
}

/** AES-256-GCM channel. Host sends with direction 1, receives direction 2. */
class SecureChannel(private val out: (ByteArray) -> Boolean, key: ByteArray) {
    private val k = SecretKeySpec(key, "AES")
    private var sendCtr = 0L
    private var recvCtr = -1L

    @Synchronized
    fun send(kind: Int, payload: ByteArray): Boolean {
        sendCtr++
        val nonce = nonce(1, sendCtr)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, k, GCMParameterSpec(128, nonce))
        val ct = c.doFinal(byteArrayOf(kind.toByte()) + payload)
        return out(nonce + ct)
    }

    fun sendJson(o: JSONObject): Boolean = send(MSG_JSON, o.toString().toByteArray())

    fun decrypt(data: ByteArray): Pair<Int, ByteArray>? {
        if (data.size < 29 || data[0] != 2.toByte()) return null
        val ctr = ByteBuffer.wrap(data, 4, 8).long
        if (ctr <= recvCtr) return null
        return try {
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, k, GCMParameterSpec(128, data.copyOfRange(0, 12)))
            val pt = c.doFinal(data, 12, data.size - 12)
            recvCtr = ctr
            Pair(pt[0].toInt() and 0xff, pt.copyOfRange(1, pt.size))
        } catch (e: Exception) {
            null
        }
    }

    companion object {
        const val MSG_JSON = 0
        const val MSG_FRAME = 1
        const val MSG_FILE = 2

        fun nonce(dir: Int, ctr: Long): ByteArray =
            ByteBuffer.allocate(12).put(dir.toByte()).put(0).put(0).put(0).putLong(ctr).array()
    }
}

/** Persistent settings (private app storage). */
class Config(ctx: Context) {
    private val p = ctx.applicationContext.getSharedPreferences("remotedesk", Context.MODE_PRIVATE)

    init {
        if (!p.contains("id")) {
            p.edit().putString("id", newId()).putString("secret", Crypto.randHex(32)).apply()
        }
    }

    var server: String
        get() = p.getString("server", DEFAULT_SERVER) ?: DEFAULT_SERVER
        set(v) = p.edit().putString("server", v).apply()
    var id: String
        get() = p.getString("id", "") ?: ""
        set(v) = p.edit().putString("id", v).apply()
    val secret: String get() = p.getString("secret", "") ?: ""
    var key: String
        get() = p.getString("key", "") ?: ""
        set(v) = p.edit().putString("key", v).apply()
    var keyId: String
        get() = p.getString("key_id", "") ?: ""
        set(v) = p.edit().putString("key_id", v).apply()
    var deviceName: String
        get() = p.getString("device_name", null) ?: (Build.MANUFACTURER.replaceFirstChar { it.uppercase() } + " " + Build.MODEL)
        set(v) = p.edit().putString("device_name", v).apply()

    fun hasPassword() = key.isNotEmpty() && keyId == id

    fun setPassword(pw: String) {
        key = Crypto.hex(Crypto.deriveKey(pw, id))
        keyId = id
    }

    fun clearPassword() {
        key = ""
        keyId = ""
    }

    @Synchronized
    fun trusted(): JSONObject = try {
        JSONObject(p.getString("trusted", "{}") ?: "{}")
    } catch (e: Exception) {
        JSONObject()
    }

    @Synchronized
    fun saveTrusted(o: JSONObject) {
        p.edit().putString("trusted", o.toString()).apply()
    }

    companion object {
        const val DEFAULT_SERVER = "wss://remotedesk-relay-tdfv.onrender.com"

        fun newId(): String = (SecureRandom().nextInt(900_000_000) + 100_000_000).toString()

        fun formatId(id: String): String =
            if (id.length == 9) "${id.substring(0, 3)} ${id.substring(3, 6)} ${id.substring(6)}" else id
    }
}
