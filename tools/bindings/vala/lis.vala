namespace Lis {
    public const string VERSION = "0.1.0";

    public class SecretRef : GLib.Object {
        public string from { get; set; }

        public SecretRef (string from_val) {
            this.from = from_val;
        }
    }

    public class KeyObject : GLib.Object {
        public string id { get; set; }
        public string? key_type { get; set; }
        public string[] purpose { get; set; }
        public SecretRef? source { get; set; }
        public bool pin_required { get; set; default = false; }

        public KeyObject (string id_val) {
            this.id = id_val;
            this.purpose = new string[0];
        }
    }

    public class User : GLib.Object {
        public string name { get; set; }
        public uint32 uid { get; set; default = 0; }
        public bool admin { get; set; default = false; }
        public string? shell { get; set; }
        public string[] groups { get; set; }
        public string[] ssh_authorized_keys { get; set; }

        public User (string name_val) {
            this.name = name_val;
            this.groups = new string[0];
            this.ssh_authorized_keys = new string[0];
        }
    }

    public class FileEntry : GLib.Object {
        public string path { get; set; }
        public string content { get; set; }
        public string? mode { get; set; }
        public string? owner { get; set; }

        public FileEntry (string path_val, string content_val) {
            this.path = path_val;
            this.content = content_val;
        }
    }

    public class Document : GLib.Object {
        public string lis { get; set; default = VERSION; }
        public string? hostname { get; set; }
        public string? timezone { get; set; }
        public string? locale { get; set; }
        public KeyObject[] keys;
        public User[] users;
        public FileEntry[] files;

        public Document () {
            this.lis = VERSION;
            this.keys = new KeyObject[0];
            this.users = new User[0];
            this.files = new FileEntry[0];
        }
    }
}
