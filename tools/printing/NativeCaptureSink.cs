// Offline x86 adapter for the inspected gslibio62 TDispIONull ABI.
// No printer, file device, USB, serial, socket, GUI or running process is opened.
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

public sealed class NativeCaptureSink : IDisposable
{
    const int NativeObjectBytes = 0x20;
    const int NativeVtableBytes = 0x74;
    const int WriteSlot = 0x30;
    const int WriteStringSlot = 0x44;
    const string ExpectedIoSha256 = "3b5c48581fd748886fe49145d1a4805e712e495ede45670c0d0703298ee52076";
    static readonly object RuntimeLock = new object();
    static IntPtr ioModule;
    static string loadedRuntime;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetDllDirectory(string path);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr LoadLibrary(string path);
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, ExactSpelling = true, SetLastError = true)]
    static extern IntPtr GetProcAddress(IntPtr module, string name);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    delegate IntPtr NullConstructor(IntPtr self, IntPtr progress);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    delegate void NullDestructor(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    delegate int WriteString(IntPtr self, IntPtr text);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    delegate int NativeWrite(IntPtr self, IntPtr data, uint length, IntPtr written);

    readonly MemoryStream captured = new MemoryStream();
    readonly object captureLock = new object();
    readonly int maximumBytes;
    readonly NativeWrite writeCallback;
    readonly NullDestructor destroy;
    readonly WriteString nativeWriteString;
    IntPtr nativeObject;
    IntPtr originalVtable;
    IntPtr captureVtable;
    bool constructed;
    bool disposed;
    string callbackError;
    int writeCalls;

    public IntPtr NativePointer { get { EnsureAlive(); return nativeObject; } }
    public string CallbackError { get { return callbackError; } }
    public int WriteCalls { get { lock (captureLock) return writeCalls; } }
    public long CapturedLength { get { lock (captureLock) return captured.Length; } }

    public static string InitializeRuntime(string runtimeDirectory)
    {
        if (IntPtr.Size != 4)
            throw new PlatformNotSupportedException("Native capture requires a separate x86 helper process.");
        string directory = Path.GetFullPath(runtimeDirectory);
        string path = Path.Combine(directory, "gslibio62.dll");
        if (!File.Exists(path)) throw new FileNotFoundException("Installed native output library not found.", path);
        string hash;
        using (SHA256 sha = SHA256.Create())
        using (FileStream stream = File.OpenRead(path))
            hash = BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        if (hash != ExpectedIoSha256)
            throw new InvalidOperationException("The output library differs from the version whose ABI was recovered.");
        lock (RuntimeLock)
        {
            if (ioModule != IntPtr.Zero)
            {
                if (!String.Equals(directory, loadedRuntime, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("One helper process can use only one native runtime directory.");
                return loadedRuntime;
            }
            if (!SetDllDirectory(directory))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "Cannot select native dependency directory.");
            ioModule = LoadLibrary(path);
            if (ioModule == IntPtr.Zero)
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "Cannot load the inspected native output library.");
            // The module remains loaded for this isolated process's lifetime.
            loadedRuntime = directory;
            return directory;
        }
    }

    static T Export<T>(string name) where T : class
    {
        IntPtr address = GetProcAddress(ioModule, name);
        if (address == IntPtr.Zero) throw new EntryPointNotFoundException(name);
        return (T)(object)Marshal.GetDelegateForFunctionPointer(address, typeof(T));
    }

    public NativeCaptureSink(string runtimeDirectory, int byteLimit = 67108864)
    {
        if (byteLimit < 1) throw new ArgumentOutOfRangeException("byteLimit");
        maximumBytes = byteLimit;
        InitializeRuntime(runtimeDirectory);
        NullConstructor create = Export<NullConstructor>("??0TDispIONull@@QAE@PAVTIndicadorEstat@@@Z");
        destroy = Export<NullDestructor>("??1TDispIONull@@UAE@XZ");
        nativeWriteString = Export<WriteString>("?WriteString@TDispIO@@UAEHPBD@Z");
        writeCallback = CaptureWrite;
        try
        {
            nativeObject = Marshal.AllocHGlobal(NativeObjectBytes);
            Marshal.Copy(new byte[NativeObjectBytes], 0, nativeObject, NativeObjectBytes);
            if (create(nativeObject, IntPtr.Zero) != nativeObject)
                throw new InvalidOperationException("Native null constructor returned an unexpected object.");
            constructed = true;
            originalVtable = Marshal.ReadIntPtr(nativeObject);
            IntPtr originalWrite = GetProcAddress(ioModule, "?Write@TDispIONull@@UAEHPAXKPAK@Z");
            IntPtr originalString = GetProcAddress(ioModule, "?WriteString@TDispIO@@UAEHPBD@Z");
            if (Marshal.ReadIntPtr(originalVtable, WriteSlot) != originalWrite ||
                Marshal.ReadIntPtr(originalVtable, WriteStringSlot) != originalString)
                throw new InvalidOperationException("Native null vtable does not match the recovered write ABI.");
            byte[] vtable = new byte[NativeVtableBytes];
            Marshal.Copy(originalVtable, vtable, 0, vtable.Length);
            captureVtable = Marshal.AllocHGlobal(vtable.Length);
            Marshal.Copy(vtable, 0, captureVtable, vtable.Length);
            Marshal.WriteIntPtr(captureVtable, WriteSlot, Marshal.GetFunctionPointerForDelegate(writeCallback));
            Marshal.WriteIntPtr(nativeObject, captureVtable);
        }
        catch
        {
            Dispose();
            throw;
        }
    }

    int CaptureWrite(IntPtr self, IntPtr data, uint length, IntPtr written)
    {
        try
        {
            if (self != nativeObject) throw new InvalidOperationException("Unexpected native sink instance.");
            if (length > Int32.MaxValue || (length != 0 && data == IntPtr.Zero))
                throw new InvalidOperationException("Invalid native write buffer.");
            lock (captureLock)
            {
                if (captured.Length + length > maximumBytes)
                    throw new InvalidOperationException("Native command capture exceeded its byte limit.");
                byte[] block = new byte[(int)length];
                if (block.Length != 0) Marshal.Copy(data, block, 0, block.Length);
                captured.Write(block, 0, block.Length);
                writeCalls++;
            }
            if (written != IntPtr.Zero) Marshal.WriteInt32(written, unchecked((int)length));
            return 0;
        }
        catch (Exception error)
        {
            callbackError = error.Message;
            if (written != IntPtr.Zero) Marshal.WriteInt32(written, 0);
            return -5; // Negative native status; never throw through an unmanaged callback.
        }
    }

    public int WriteAsciiThroughNative(string text)
    {
        EnsureAlive();
        if (text == null || text.IndexOf('\0') >= 0)
            throw new ArgumentException("A non-null ASCII string without embedded NUL is required.");
        foreach (char value in text)
            if (value > 127) throw new ArgumentException("ASCII input is required.");
        byte[] bytes = Encoding.ASCII.GetBytes(text + "\0");
        IntPtr buffer = Marshal.AllocHGlobal(bytes.Length);
        try
        {
            Marshal.Copy(bytes, 0, buffer, bytes.Length);
            int result = nativeWriteString(nativeObject, buffer);
            GC.KeepAlive(writeCallback);
            return result;
        }
        finally { Marshal.FreeHGlobal(buffer); }
    }

    public byte[] GetCapturedBytes()
    {
        EnsureAlive();
        lock (captureLock) return captured.ToArray();
    }

    public void Clear()
    {
        EnsureAlive();
        lock (captureLock)
        {
            captured.SetLength(0);
            writeCalls = 0;
            callbackError = null;
        }
    }

    void EnsureAlive()
    {
        if (disposed) throw new ObjectDisposedException("NativeCaptureSink");
    }

    public void Dispose()
    {
        if (disposed) return;
        disposed = true;
        try
        {
            if (constructed && nativeObject != IntPtr.Zero)
            {
                if (originalVtable != IntPtr.Zero) Marshal.WriteIntPtr(nativeObject, originalVtable);
                destroy(nativeObject); // Non-deleting destructor; allocation remains ours.
            }
            GC.KeepAlive(writeCallback);
        }
        finally
        {
            if (nativeObject != IntPtr.Zero) Marshal.FreeHGlobal(nativeObject);
            if (captureVtable != IntPtr.Zero) Marshal.FreeHGlobal(captureVtable);
            nativeObject = captureVtable = IntPtr.Zero;
            captured.Dispose();
        }
    }
}

#if NATIVE_CAPTURE_SMOKE
public static class NativeCaptureSmoke
{
    public static int Main(string[] args)
    {
        try
        {
            if (args.Length != 1) throw new ArgumentException("Pass the installed native library directory.");
            using (NativeCaptureSink sink = new NativeCaptureSink(args[0]))
            {
                string fixture = "IN;PU;";
                int status = sink.WriteAsciiThroughNative(fixture);
                string actual = Encoding.ASCII.GetString(sink.GetCapturedBytes());
                if (status != 0 || actual != fixture || sink.WriteCalls != 1 || sink.CallbackError != null)
                    throw new InvalidOperationException("The native capture smoke fixture did not match.");
                Console.WriteLine("{\"passed\":true,\"architecture\":\"x86\",\"bytes\":" + sink.CapturedLength +
                    ",\"write_calls\":" + sink.WriteCalls + ",\"captured_ascii\":\"" + actual +
                    "\",\"hardware_transport\":false,\"native_calls\":\"TDispIONull constructor, TDispIO WriteString, callback, destructor\"}");
            }
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.GetType().Name + ": " + error.Message);
            return 1;
        }
    }
}
#endif
