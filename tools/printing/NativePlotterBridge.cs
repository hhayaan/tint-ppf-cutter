// Isolated x86 command compiler. The only I/O object supplied to vendor plotters
// is NativeCaptureSink's real NULL device with an in-memory Write callback.
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Web.Script.Serialization;

public static class NativePlotterBridge
{
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr LoadLibrary(string file);
    [DllImport("kernel32.dll", CharSet=CharSet.Ansi, ExactSpelling=true)]
    static extern IntPtr GetProcAddress(IntPtr module, string name);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate IntPtr Factory(int index);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate IntPtr MakeConfig(IntPtr self, IntPtr job, IntPtr options);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate IntPtr MakePlotter(IntPtr self, IntPtr io, IntPtr config);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate int NoArgs(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate int OneInt(IntPtr self, int value);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate int OneDouble(IntPtr self, double value);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate void SetInt(IntPtr self, int value);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate int PointerArg(IntPtr self, IntPtr value);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate int IsType(IntPtr self, IntPtr type);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate int Pen(IntPtr self, IntPtr point, IntPtr angle);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate int Move(IntPtr self, double x, double y, IntPtr angle);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate IntPtr ConvertPoint(IntPtr self, IntPtr result, double x, double y);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate void InversePoint(IntPtr self, IntPtr result, int x, int y);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate IntPtr RectangleCtor(IntPtr self, double x0, double y0, double x1, double y1);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] delegate void Delete(IntPtr self, uint flags);

    static T At<T>(IntPtr address) where T:class {
        if(address==IntPtr.Zero) throw new InvalidOperationException("Missing native entry point.");
        return (T)(object)Marshal.GetDelegateForFunctionPointer(address,typeof(T));
    }
    static T Virtual<T>(IntPtr self,int offset) where T:class {
        return At<T>(Marshal.ReadIntPtr(Marshal.ReadIntPtr(self),offset));
    }
    static void Check(int status,string operation) {
        if(status<0) throw new InvalidOperationException(operation+" failed with native status "+status);
    }
    static string Hash(string file) {
        using(var sha=SHA256.Create()) using(var s=File.OpenRead(file))
            return BitConverter.ToString(sha.ComputeHash(s)).Replace("-", "").ToLowerInvariant();
    }
    static double Number(Dictionary<string,object> d,string key) {
        double v=Convert.ToDouble(d[key],System.Globalization.CultureInfo.InvariantCulture);
        if(Double.IsNaN(v)||Double.IsInfinity(v)) throw new ArgumentException("Non-finite "+key);
        return v;
    }
    static void StoreDouble(IntPtr p,int offset,double value) {
        Marshal.Copy(BitConverter.GetBytes(value),0,IntPtr.Add(p,offset),8);
    }
    static double ReadDouble(IntPtr p,int offset) {
        byte[] b=new byte[8]; Marshal.Copy(IntPtr.Add(p,offset),b,0,8); return BitConverter.ToDouble(b,0);
    }
    static void DeleteNative(IntPtr p) {
        if(p!=IntPtr.Zero) Virtual<Delete>(p,0)(p,1);
    }

    public static int Main(string[] args) {
        try {
            if(args.Length!=3) throw new ArgumentException("Usage: NativePlotterBridge request.json output.bin receipt.json");
            var json=new JavaScriptSerializer(); json.MaxJsonLength=134217728;
            var request=json.Deserialize<Dictionary<string,object>>(File.ReadAllText(args[0]));
            var receipt=Compile(request,args[1]);
            File.WriteAllText(args[2],json.Serialize(receipt));
            Console.WriteLine("Captured "+receipt["bytes"]+" command bytes into a file; no hardware transport opened.");
            return 0;
        } catch(Exception e) {Console.Error.WriteLine(e.ToString());return 1;}
    }

    static Dictionary<string,object> Compile(Dictionary<string,object> request,string output) {
        if((string)request["schema"]!="tint-ppf-native-job.v1") throw new ArgumentException("Unsupported request schema.");
        string runtime=NativeCaptureSink.InitializeRuntime((string)request["runtime_directory"]);
        string shared=Path.Combine(runtime,"gsfwkcut62.dll");
        if(Hash(shared)!="227203a783a8b4774d5bdc55ce090488db8b76b7f96abe3c9aa0475b0be5c868")
            throw new InvalidOperationException("Unsupported shared plotter library version.");
        string name=(string)request["driver_filename"];
        if(Path.GetFileName(name)!=name||!name.EndsWith(".dll",StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Flat driver filename required.");
        string driverPath=Path.Combine(runtime,"drvcut",name);
        if(Hash(driverPath)!=(string)request["driver_sha256"]) throw new InvalidOperationException("Driver version differs from the inspected catalog.");
        IntPtr module=LoadLibrary(driverPath);
        if(module==IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(),"Cannot load model driver.");
        IntPtr nativeDriver=At<Factory>(GetProcAddress(module,"__GetDriverTall"))(Convert.ToInt32(request["variant_index"]));
        if(nativeDriver==IntPtr.Zero) throw new InvalidOperationException("Driver factory returned NULL.");
        // CString's character pointer is observed at +0x10 in the inspected TDriver layout.
        string actualId=Marshal.PtrToStringUni(Marshal.ReadIntPtr(nativeDriver,0x10));
        if(actualId!=(string)request["model_id"]) throw new InvalidOperationException("Factory index/model ID mismatch: "+actualId);
        bool craftInverse=String.Equals(name,"CraftRobo.dll",StringComparison.OrdinalIgnoreCase)
            && (string)request["driver_sha256"]=="03ba2009ed19895f720d4149fe81fe9d90186016265b7338b7a02ba24347a85d"
            && actualId=="craftrobo_cc100_20" && Convert.ToInt32(request["variant_index"])==0;
        // Never poll a physical device. This flag is recovered from the driver constructor.
        Marshal.WriteInt32(nativeDriver,0x130,0);
        IntPtr config=IntPtr.Zero,plotter=IntPtr.Zero,rectangle=IntPtr.Zero,nativePoint=IntPtr.Zero,inverseSourcePoint=IntPtr.Zero;
        using(var sink=new NativeCaptureSink(runtime)) {
            try {
                config=Virtual<MakeConfig>(nativeDriver,0x44)(nativeDriver,IntPtr.Zero,IntPtr.Zero);
                if(config==IntPtr.Zero) throw new InvalidOperationException("No native cut configuration.");
                // Explicit host conditions only; ignore saved user's speed/force/accel settings.
                Marshal.WriteInt32(config,0x10c,0); Marshal.WriteInt32(config,0x114,0); Marshal.WriteInt32(config,0x11c,0);
                Marshal.WriteInt32(config,0x190,0); // Do not inherit an optical registration request.
                Marshal.WriteInt32(config,0x13c,1); // Deterministic absolute-coordinate cut mode.
                StoreDouble(config,0xc0,0); // No implicit inter-page feed offset.
                Marshal.WriteInt32(config,0x1f8,0); // No sheet-cut request.
                var options=(Dictionary<string,object>)request["options"];
                IntPtr framework=LoadLibrary(shared);
                IntPtr library=LoadLibrary(Path.Combine(runtime,"gslib62.dll"));
                var isType=At<IsType>(GetProcAddress(library,"?EsTipus@TItem@@QBEHPBVCCiberRTTI@@@Z"));
                bool isHpgl=isType(config,GetProcAddress(framework,"?m_rttiTConfigDriverTallHPGL@TConfigDriverTallHPGL@@2VCCiberRTTI@@A"))!=0;
                bool isDmpl=isType(config,GetProcAddress(framework,"?m_rttiTConfigDriverTallDMPL@TConfigDriverTallDMPL@@2VCCiberRTTI@@A"))!=0;
                if(isHpgl==isDmpl || ((string)request["family"]=="DMPL")!=isDmpl)
                    throw new InvalidOperationException("Native configuration ancestry differs from the recovered ABI family.");
                IntPtr rotate=GetProcAddress(framework,"?SetRotate90@TConfigDriverTallHPGL@@QAEXH@Z");
                // The HPGL-specific field is used only for HPGL families.
                if(isHpgl) At<SetInt>(rotate)(config,Convert.ToBoolean(options["rotate_axes"])?1:0);
                double ppmX=ReadDouble(config,0x128),ppmY=ReadDouble(config,0x130);
                if(ppmX<=0||ppmY<=0||Double.IsNaN(ppmX)||Double.IsNaN(ppmY)||Double.IsInfinity(ppmX)||Double.IsInfinity(ppmY)) throw new InvalidOperationException("Invalid driver coordinate scale.");
                plotter=Virtual<MakePlotter>(nativeDriver,0xbc)(nativeDriver,sink.NativePointer,config);
                if(plotter==IntPtr.Zero) throw new InvalidOperationException("No native plotter.");
                if(Marshal.ReadIntPtr(plotter,8)!=sink.NativePointer)
                    throw new InvalidOperationException("Native plotter replaced the supplied memory-only I/O sink.");
                double width=Number(request,"width_mm"),height=Number(request,"height_mm");
                if(width<=0||height<=0||width>100000||height>100000) throw new ArgumentException("Invalid page dimensions.");
                const double pointsPerMm=72.0/25.4;
                IntPtr cad=LoadLibrary(Path.Combine(runtime,"gslibcad62.dll"));
                rectangle=Marshal.AllocHGlobal(40);
                At<RectangleCtor>(GetProcAddress(cad,"??0TRectangle@@QAE@NNNN@Z"))(rectangle,0,0,width*pointsPerMm,height*pointsPerMm);
                Check(Virtual<NoArgs>(plotter,0x28)(plotter),"Begin job");
                Check(Virtual<PointerArg>(plotter,0x30)(plotter,rectangle),"Begin page");
                Check(Virtual<OneInt>(plotter,0x40)(plotter,Convert.ToInt32(options["tool"])),"Select tool");
                if(options["speed"]!=null) Check(Virtual<OneInt>(plotter,0x44)(plotter,Convert.ToInt32(options["speed"])),"Set speed");
                if(options["force"]!=null) Check(Virtual<OneInt>(plotter,0x48)(plotter,Convert.ToInt32(options["force"])),"Set force");
                if(options["acceleration"]!=null) Check(Virtual<OneDouble>(plotter,0x4c)(plotter,Number(options,"acceleration")),"Set acceleration");
                var up=Virtual<Pen>(plotter,0x70);var down=Virtual<Pen>(plotter,0x74);var move=Virtual<Move>(plotter,0x78);
                var convert=Virtual<ConvertPoint>(plotter,0x20);
                InversePoint inverse=null;
                if(craftInverse) {
                    inverse=Virtual<InversePoint>(plotter,0x24);
                    inverseSourcePoint=Marshal.AllocHGlobal(16);
                }
                nativePoint=Marshal.AllocHGlobal(8);
                double minX=Double.PositiveInfinity,minY=Double.PositiveInfinity,maxX=Double.NegativeInfinity,maxY=Double.NegativeInfinity;
                int pointCount=0,pathCount=0;
                foreach(object rawPath in (IEnumerable)request["paths"]) {
                    var path=(IEnumerable)rawPath; bool first=true;int count=0;
                    Check(up(plotter,IntPtr.Zero,IntPtr.Zero),"Lift tool");
                    foreach(object rawPoint in path) {
                        var pair=(IList)rawPoint; double x=Convert.ToDouble(pair[0]),y=Convert.ToDouble(pair[1]);
                        if(Double.IsNaN(x)||Double.IsInfinity(x)||Double.IsNaN(y)||Double.IsInfinity(y)||x<0||y<0||x>width+0.000001||y>height+0.000001) throw new ArgumentException("Point outside prepared page.");
                        if(++pointCount>2000000) throw new ArgumentException("Too many points.");
                        double sourceX=x*pointsPerMm,sourceY=y*pointsPerMm;
                        int wantedX=0,wantedY=0;
                        if(craftInverse) {
                            // This inspected model always swaps/inverts axes. Its original
                            // inverse converter maps the desired machine frame back into
                            // the exact source points consumed by its original MoveTo.
                            wantedX=checked((int)Math.Round(x*ppmX,MidpointRounding.AwayFromZero));
                            wantedY=checked((int)Math.Round(y*ppmY,MidpointRounding.AwayFromZero));
                            inverse(plotter,inverseSourcePoint,wantedX,wantedY);
                            sourceX=ReadDouble(inverseSourcePoint,0);sourceY=ReadDouble(inverseSourcePoint,8);
                            if(Double.IsNaN(sourceX)||Double.IsInfinity(sourceX)||Double.IsNaN(sourceY)||Double.IsInfinity(sourceY))
                                throw new InvalidOperationException("Native inverse produced invalid source coordinates.");
                        }
                        convert(plotter,nativePoint,sourceX,sourceY);
                        if(craftInverse && (Marshal.ReadInt32(nativePoint)!=wantedX || Marshal.ReadInt32(nativePoint,4)!=wantedY))
                            throw new InvalidOperationException("Native inverse coordinate conversion did not round-trip.");
                        double px=Marshal.ReadInt32(nativePoint)/ppmX,py=Marshal.ReadInt32(nativePoint,4)/ppmY;
                        minX=Math.Min(minX,px);minY=Math.Min(minY,py);maxX=Math.Max(maxX,px);maxY=Math.Max(maxY,py);
                        Check(move(plotter,sourceX,sourceY,IntPtr.Zero),"Move");
                        if(first) {Check(down(plotter,IntPtr.Zero,IntPtr.Zero),"Lower tool"); first=false;}
                        count++;
                    }
                    if(count<2) throw new ArgumentException("A path needs at least two points.");
                    Check(up(plotter,IntPtr.Zero,IntPtr.Zero),"Lift tool");pathCount++;
                }
                if(pathCount==0) throw new ArgumentException("Empty job.");
                Check(Virtual<OneInt>(plotter,0x34)(plotter,Convert.ToBoolean(options["return_origin"])?1:0),"End page");
                Check(Virtual<NoArgs>(plotter,0x2c)(plotter),"End job");
                if(sink.CallbackError!=null) throw new InvalidOperationException(sink.CallbackError);
                byte[] payload=sink.GetCapturedBytes();
                using(var file=new FileStream(output,FileMode.CreateNew,FileAccess.Write,FileShare.None)) file.Write(payload,0,payload.Length);
                return new Dictionary<string,object>{{"schema","tint-ppf-native-receipt.v1"},{"model_id",actualId},{"bytes",payload.Length},{"sha256",Hash(output)},{"path_count",pathCount},{"point_count",pointCount},{"native_units_per_mm_x",ppmX},{"native_units_per_mm_y",ppmY},{"native_bounds_mm",new double[]{minX,minY,maxX,maxY}},{"coordinate_mode",craftInverse?"native-inverse-direct-xy":"native-source-points"},{"native_abi_family",isDmpl?"DMPL":"HPGL"},{"hardware_opened",false},{"write_calls",sink.WriteCalls},{"physical_completion_verified",false}};
            } finally {
                if(nativePoint!=IntPtr.Zero) Marshal.FreeHGlobal(nativePoint);
                if(inverseSourcePoint!=IntPtr.Zero) Marshal.FreeHGlobal(inverseSourcePoint);
                if(rectangle!=IntPtr.Zero) Marshal.FreeHGlobal(rectangle);
                DeleteNative(plotter);DeleteNative(config);
            }
        }
    }
}
