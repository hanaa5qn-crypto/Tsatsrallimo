import { StatusBar } from 'expo-status-bar';
import { StyleSheet, Text, View } from 'react-native';

export default function App() {
  return (
    <View style={styles.container}>
      <Text style={styles.title}>Tsatsral Limo</Text>
      <Text style={styles.subtitle}>Mobile app — live reload working.</Text>
      <StatusBar style="auto" />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#0b0b0b',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
  },
  title: {
    color: '#f5c542',
    fontSize: 36,
    fontWeight: '700',
    letterSpacing: 1,
  },
  subtitle: {
    color: '#cccccc',
    fontSize: 16,
    marginTop: 12,
  },
});
