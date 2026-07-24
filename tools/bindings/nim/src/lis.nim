import json, options

const Version* = "0.1.0"

type
  SecretRef* = object
    fromRef*: string

  KeyObject* = object
    id*: string
    keyType*: Option[string]
    purpose*: seq[string]
    pinRequired*: bool

  TargetDisk* = object
    id*: string

  Target* = object
    arch*: Option[string]
    firmware*: Option[string]
    disks*: seq[TargetDisk]

  Partition* = object
    disk*: string
    id*: Option[string]
    role*: Option[string]
    size*: Option[string]
    fs*: Option[string]
    mountpoint*: Option[string]

  Storage* = object
    partitions*: seq[Partition]

  User* = object
    name*: string
    uid*: Option[int]
    admin*: bool
    shell*: Option[string]
    groups*: seq[string]

  Software* = object
    role*: Option[string]
    packages*: seq[string]

  Document* = object
    lis*: string
    meta*: Option[JsonNode]
    keys*: seq[KeyObject]
    target*: Option[Target]
    storage*: Option[Storage]
    users*: seq[User]
    software*: Option[Software]

proc parseDocument*(jsonStr: string): Document =
  let node = parseJson(jsonStr)
  result.lis = node.getOrDefault("lis").getStr(Version)
  if node.hasKey("keys"):
    for k in node["keys"]:
      var key: KeyObject
      key.id = k.getOrDefault("id").getStr("")
      if k.hasKey("type"): key.keyType = some(k["type"].getStr())
      result.keys.add(key)

proc toXmlOrJson*(doc: Document): string =
  return $(%*{"lis": doc.lis})
